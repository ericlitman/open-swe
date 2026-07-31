# AGENTS.md

This file provides guidance to Coding Agents when working with code in this repository.

## Project

Open SWE is an open-source coding-agent framework built on **LangGraph** + **Deep Agents** (`deepagents.create_deep_agent`). The main coding agent is invoked from Slack, Linear, GitHub, the dashboard, and scheduled runs. Coding, reviewer, and analyzer runs use per-thread sandboxes; the PR chat graph is deliberately sandbox-less.

The application also provides stock and adversarial PR reviewers, a review-style analyzer, PR chat, and a scheduler/reconciliation graph.

## Commands

Dependencies are managed with **uv**. Tests use pytest (`asyncio_mode = "auto"`). Lint/format is **ruff** (line-length 100, target py311). Type checking is **basedpyright** (`typeCheckingMode = "standard"`). `requires-python = ">=3.11"`; `langgraph.json` pins the runtime to 3.12.

```bash
make install            # uv sync --extra dev (pytest, ruff, …)
make dev                # uv run langgraph dev — serves all six graphs + the FastAPI app from langgraph.json
make run                # uvicorn agent.webapp:app --reload --port 8000 (FastAPI only, no LangGraph runtime)
make test               # uv run pytest -vvv tests/
make test TEST_FILE=tests/github/test_open_pull_request.py    # single test file
uv run pytest -vvv tests/github/test_open_pull_request.py::test_name  # single test
make lint               # ruff check + ruff format --diff
make format             # ruff format + ruff check --fix
make typecheck          # basedpyright agent tests
```

`langgraph.json` declares six graph entrypoints and the FastAPI app, all served together by `langgraph dev`:

| Graph | Entrypoint | Purpose |
|---|---|---|
| `agent` | `agent.graphs.agent:traced_agent` | Main coding agent (Slack/Linear/GitHub/dashboard/scheduled triggers). |
| `reviewer` | `agent.graphs.reviewer:traced_reviewer_agent` | Stock read-only PR reviewer. Findings model + `publish_review`. |
| `reviewer_adversarial` | `agent.graphs.reviewer_adversarial:traced_reviewer_adversarial` | First-review adversarial variant selectable through `agent/dashboard/team_settings.py:REVIEWER_ROUTING_VALUES`; follow-up review flows use the stock reviewer. |
| `analyzer` | `agent.graphs.analyzer:traced_analyzer` | Learns per-repo reviewer style from historical PRs and reviewer finding outcomes. |
| `chat` | `agent.graphs.chat:traced_chat_agent` | Read-only, sandbox-less PR chat over the diff, published findings, and GitHub API. |
| `scheduler` | `agent.graphs.scheduler:get_scheduler` | Fans scheduled ticks into fresh agent threads; `task="reconcile"` sweeps stale runs and auto-merge PRs. |

The FastAPI compatibility entrypoint is `agent.webapp:app`.

Review-driven auto-fix is produced by `agent/tools/publish_review.py:_maybe_dispatch_review_autofix` after a review publishes qualifying findings. Dispatch requires the team auto-fix setting and severity threshold, an enabled repository, a local PR head tied to an implementation thread, the persisted per-PR opt-out to be clear, the per-user `auto_fix_ci` profile flag not to disable it, and fewer than two prior successful dispatches. The producer stores a pending event consumed by `check_message_queue_before_model`, dispatches the implementation thread, and reports dispatch, failure, and cycle-limit status with `post_autofix_status_check`. PR comments can disable review-driven auto-fix for that PR with `@open-swe autofix off` and re-enable it with `@open-swe autofix on`. PR #1621 deliberately removed the CI-failure-driven `ci_monitor` / `agent/ci_autofix.py` path and CI-failure webhook handling; do not reintroduce that subsystem.

## Architecture

### Entrypoints

- **`agent/graphs/agent.py` → `agent.server.get_agent(config)`** — re-export shim for the main graph factory. Per execution it resolves main-agent GitHub auth, sandbox state, model/profile/plan policy, optional tools and skills, then constructs a fresh `create_deep_agent(...)`.
- **`agent/graphs/reviewer.py` → `agent.reviewer:get_reviewer_agent(config)`** — stock reviewer graph. It shares the sandbox runtime but uses reviewer-only tools, subagent, prompt, and middleware; it cannot commit, push, or open PRs.
- **`agent/graphs/reviewer_adversarial.py` → `agent.reviewer_adversarial:get_reviewer_adversarial_agent(config)`** — first-review adversarial pipeline with independent finder/gate stages. Team reviewer routing may select it for first reviews; re-reviews and finding replies route to the stock reviewer.
- **`agent/graphs/analyzer.py` → `agent.analyzer:get_analyzer(config)`** — review-style analyzer. Bootstrap and continual procedures are deepagents skills; output is saved with `save_review_style_prompt` and consumed by reviewers.
- **`agent/graphs/chat.py` → `agent.chat:get_chat_agent(config)`** — read-only PR chat. It has no sandbox or mutation tools and reads virtual `/pr/` context plus GitHub-backed repository data.
- **`agent/graphs/scheduler.py` → `agent.scheduler:get_scheduler(config)`** — launches configured schedules through `agent/dashboard/schedules.py`; reconciliation ticks call `reconcile_stale_runs` and `reconcile_auto_merge_prs`.
- **`agent/webapp.py` / `agent/api/app.py`** — `webapp.py` is a compatibility shim; `api/app.py` composes dashboard, plan, workflow-approval, health/completion, and webhook routers. Source HTTP routes live in `agent/webhooks/{github,linear,slack}_routes.py`, with processing in the corresponding service modules and shared helpers in `agent/webhooks/common.py`.
- **`agent/dispatch.py` / `agent/completion.py`** — webhook, plan-approval, review-auto-fix, scheduled-agent, and analyzer launches use durable dispatch defaults (`multitask_strategy="interrupt"`, `durability="sync"`). Dashboard `run.start` commands use the dashboard command proxy instead; busy dashboard follow-ups are queued. When an absolute non-loopback completion URL and `RUN_COMPLETE_WEBHOOK_SECRET` are configured, dispatch attaches `/webhooks/run-complete`; the authenticated handler settles deferred review checks and posts idempotent failure replies for error/timeout runs.
- **`agent/dashboard/`** — dashboard API, OAuth and integration settings, user profiles, team defaults, enabled repositories, schedules, review-style jobs, and UI thread APIs.

### Sandbox lifecycle (the tricky part)

`SANDBOX_BACKENDS` in `agent/utils/sandbox_state.py` is an in-process `thread_id` → stable `SandboxBackendProxy` cache. Thread metadata persists the real `sandbox_id` across processes; the proxy can reconnect or replace its underlying provider backend without invalidating references held by middleware. `agent/runtime/sandbox.py` exposes lazy wrappers around the implementation in `agent/server.py`.

`ensure_sandbox_for_thread` has three cases. Durable interrupt dispatch prevents concurrent provisioning for one thread, so there is no cross-process creation sentinel:

1. A cached backend exists → ping it, recreate on `SandboxClientError`, and refresh the GitHub proxy (recreating if refresh fails).
2. Metadata has a sandbox id but no live backend → reconnect; create a replacement on connection failure, then health-check and refresh it.
3. Neither cache nor metadata has a sandbox → create one and persist its id.

For `SANDBOX_TYPE=langsmith` (the default), creation and healthy reuse configure the provider-side GitHub proxy with a repository-scoped GitHub App installation token; before-model middleware refreshes it near expiry. Other providers (`daytona`, `modal`, `runloop`, `e2b`, `local`) do not use that proxy. Provider selection and factories live in `agent/utils/sandbox.py`. Every run reapplies the configured global git identity because reused/reconnected sandboxes can lose it and preview systems may require a GitHub-resolvable author.

### Middleware stacks (order matters)

The main stack in `agent/server.py:get_agent` is wired in this order:

1. `PrepareAgentRunMiddleware` prepares auth, sandbox/repository state, prompt, and run policy.
2. `TrustedSkillsMiddleware` is included only when trusted repository skill sources were prepared.
3. `SanitizeToolInputsMiddleware`.
4. `ModelCallLimitMiddleware` with `exit_behavior="end"`.
5. `ToolErrorMiddleware`.
6. `SubdirAgentsReadMiddleware` appends applicable ancestor `AGENTS.md` instructions to file reads.
7. `ToolRetryMiddleware` retries the `task` tool only.
8. `ToolArtifactMiddleware` handles oversized tool outputs/artifacts.
9. `PullRequestCreationGuardMiddleware` blocks shell/API fallbacks that would bypass attributed `open_pull_request` creation.
10. `SourceCompletionGuardMiddleware` records when a successful PR open was not followed by a source-channel reply; it does not open a PR or post the reply itself.
11. `refresh_github_proxy_before_model`.
12. `check_message_queue_before_model` injects queued dashboard/source messages and pending review auto-fix events.
13. `SlackAssistantStatusMiddleware`.
14. `TimeoutWrapupMiddleware`.
15. `notify_step_limit_reached`.
16. Optional `ModelFallbackMiddleware`.
17. `PlanModeMiddleware` (always installed and state-aware; its model/tool restrictions are conditional).
18. `SanitizeFireworksMessagesMiddleware`.
19. `SanitizeThinkingBlocksMiddleware`.

The stock reviewer stack in `agent/reviewer.py` is: `PrepareReviewerRunMiddleware`, `SanitizeToolInputsMiddleware`, `ModelCallLimitMiddleware`, `ToolErrorMiddleware`, `refresh_github_proxy_before_model`, `check_message_queue_before_model`, `SlackAssistantStatusMiddleware`, `TimeoutWrapupMiddleware`, optional profile-driven `ExcludeToolsMiddleware`, `SanitizeFireworksMessagesMiddleware`, `SanitizeThinkingBlocksMiddleware`, `RepairOrphanedToolCallsMiddleware`, and `settle_review_check_on_exit`.

No after-agent middleware creates a PR. The model must commit, push, call `open_pull_request`, and notify the source. Auto-merge eligibility is evaluated during run preparation and PR creation; `open_pull_request` records eligible intent, while scheduler reconciliation observes Mergify state. The PR creation guard only prevents unattributed fallback creation paths.

### Tools, subagents, and skills

All fixed first-party graph tools live in `agent/tools/` and are flat-imported through `agent/tools/__init__.py`. The fixed main-agent list is:
`http_request`, `fetch_url`, `web_search`, `approve_plan`, `enter_plan_mode`, `save_plan`, `linear_comment`, `linear_create_issue`, `linear_delete_issue`, `linear_get_issue`, `linear_get_issue_comments`, `linear_list_teams`, `linear_search_issues`, `linear_update_issue`, `open_pull_request`, `request_pr_review`, `report_platform_issue`, `schedule_thread_wakeup`, `slack_add_reaction`, `slack_read_thread_messages`, `slack_start_new_thread`, `slack_thread_reply`.

The server conditionally appends Corridor MCP tools; authorized Datadog/LangSmith observability tools; user-connected Currents and Notion tools; and a Stagehand browser subagent when browser tooling is configured. Tool-load failures degrade to an empty optional group. The general-purpose subagent is always present.

For a trusted configured or team-default repository, `prepare_main_agent_repo_skills` clones/prepares the repository and discovers trusted `.agents/skills` sources. Those sources are passed to the main agent and general-purpose subagent, with `TrustedSkillsMiddleware` enforcing the prepared ref. Built-in deepagents filesystem/shell/task tools are supplied by `create_deep_agent`; do not duplicate them in the project list.

Stock reviewer tools are `fetch_review_diff`, `add_finding`, `update_finding`, `list_findings`, `publish_review`, `resolve_finding_thread`, `reply_to_finding_thread`, `web_search`, `fetch_url`, and `http_request`. The analyzer uses `save_review_style_prompt` and `read_finding_outcomes`; PR chat uses its read-only GitHub/review tools.

### Models, profiles, and team defaults

The main and general-purpose subagent start from separate team default model/effort pairs. User profile overrides can replace both and can independently override the subagent. An active plan-stage profile can replace the main model in plan mode; a valid per-thread `agent_model_id` + `agent_effort` override is applied after profile and stage-profile selection to both, then Fable gating validates or replaces gated choices. Supported IDs, effort validation, Fable gating, and per-stage defaults live in `agent/dashboard/options.py`, `agent/dashboard/team_settings.py`, and `agent/utils/stage_profiles.py`. Model construction and fallback selection live in `agent/utils/model.py`.

Profile/team settings also control Always Create PRs, plan approval, auto-merge eligibility, reviewer routing, review auto-fix, gateway routing, and optional integrations.

### Auth

- **Main coding agent GitHub execution**: `agent/utils/auth.py:resolve_github_token` resolves and caches only the trusted repository GitHub App installation token. Dashboard OAuth credentials are used for viewer identity and repository-access checks, not as main-agent execution credentials. Installation tokens also configure repository-scoped LangSmith sandbox proxy access.
- **Webhooks and completion**: source route modules verify GitHub, Linear, and Slack signatures through shared utilities. `/webhooks/run-complete` separately fails closed unless its configured shared-secret token matches.
- **Dashboard / UI**: GitHub OAuth login and callbacks live under `agent/dashboard/`; integration credentials and user connections are loaded server-side for their specific optional tools.

### Thread and run routing

Source handlers derive deterministic thread ids so a Linear issue, Slack thread, GitHub issue/PR branch, or reviewer PR returns to its existing thread. Reviewer and Slack helper IDs live in `agent/utils/thread_ids.py`; branch-derived agent IDs live in `agent/utils/github_comments.py`. `dispatch_agent_run` interrupts an active run and starts its replacement with full checkpointed history plus the new message, while idle threads start normally.

## Conventions

- Tests are unit-only by default (`tests/`). Integration tests belong under `tests/integration_tests/`; `make integration_tests` skips when that path is absent.
- New sandbox providers: add an integration module and register its lazy factory in `agent/utils/sandbox.py:SANDBOX_FACTORIES`. See `docs/CUSTOMIZATION.md`.
- New tools: add to `agent/tools/`, export from `agent/tools/__init__.py`, and wire into the relevant graph factory or conditional loader.
- New middleware: add to `agent/middleware/`, export from `agent/middleware/__init__.py`, and wire it into the relevant ordered stack.
- Async-only: do not add sync/async dual implementations. Implement the async variant; only define a sync stub that raises `NotImplementedError` when an interface requires one.
- New FastAPI routes: define an `APIRouter` in the owning `agent/api/`, `agent/webhooks/`, or `agent/dashboard/` module and compose it in `agent/api/app.py` when it is not already included by a parent router.
- New graphs: add the implementation, an `agent/graphs/` re-export shim, and the `langgraph.json` entrypoint.
- Minimal-to-no code comments — only when the *why* is not obvious from the code.

<!-- OPENWIKI:START -->

## OpenWiki

This repository uses OpenWiki for recurring code documentation. Start with `openwiki/quickstart.md`, then follow its links to architecture, workflows, domain concepts, operations, integrations, testing guidance, and source maps.

The scheduled OpenWiki GitHub Actions workflow refreshes the repository wiki. Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
