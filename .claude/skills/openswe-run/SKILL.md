---
name: openswe-run
description: Dispatch one Open SWE ticket or one atomic ticket bundle via the @openswe Linear-comment path, watch it to a terminal state with minimal orchestration tokens, adjudicate the plan gate at checklist weight, and keep a dogfood log of process/substrate/ergonomics issues. Use openswe-wave for independent multi-ticket wave operations.
---

# openswe-run: execute one ticket or atomic bundle

## Loading contract

At invocation, load only this `SKILL.md`. Treat every file under `scripts/*` as a black-box CLI: run it and consume its output, but never read it into context. Both `scripts/openswe_run.py` and the sibling wave engine `../openswe-wave/scripts/openswe_wave.py` are implementation details; reading either is a defect, not diligence.

Load references only at their workflow event:

- Read `references/run-templates.md` only when composing the relevant dispatch, approval, rejection, or other operator comment.
- Read `../openswe-wave/references/adjudication-checklist.md` only after `plan_posted`, when adjudicating the plan, and again when spot-auditing the opened PR's diff.
- Read `../openswe-wave/references/recovery-runbook.md` only after a stall, merge conflict, queue stall, or other recovery event.

Single-run sibling of `openswe-wave`. Dispatch → watch → adjudicate plan → watch → report. A bundle remains one run: the first ticket is primary, included tickets share its Linear thread, branch, plan, PR, and monitor.
Use `openswe-bundle` first when deciding whether tickets belong in one atomic run or separate waves.
Run state changes go through an `@openswe` Linear comment (the product path — runs stay
operator-observable in the dashboard). Never create, resume, or mutate LangGraph runs via the
SDK/API; read-only status queries are what the bundled monitor already does for you.

The exceptions are `approve` and `reject`, which transition the plan record before they
comment. A comment alone makes the agent act but never transitions the plan, so they perform
the same plan-store transitions as the dashboard — `approved` and `revising` — and only then
post the comment. Order matters: the Linear webhook reads the record when dispatching, and PR
creation re-checks it, so a later write is too late.

Both fail closed. Everything that can refuse the operation — issue lookup, the placeholder
guard, the adjudication flag — runs *before* the write, because the write is not rolled back;
and the write itself refuses a thread with no stored plan, or a `shared`/`cancelled` one, as
the dashboard does. `reject` returns the record to `revising` so a rejection posted after an
approval cannot leave standing implementation authorization.

Plan-gated automatic merge now works through this comment path when team settings use
`auto_merge_mode=on_plan_approval` with `require_plan_approval=true`. After `approve` stores
the `approved` record, the Linear webhook dispatches the comment with
`plan_gate_bypass=True`; the server therefore resumes implementation instead of forcing
another plan round. The approved run is merge-eligible, so a default-branch PR opens
**born-ready** with Mergify reconciliation intent. Mergify, not Open SWE, admits and merges it
after required checks and Open SWE Review pass. Manual ready-for-review recovery is not part
of this path.

With `require_plan_approval=false`, `on_plan_approval` remains intentionally ineligible because
the product plan gate is disabled. `auto_merge_mode=always` remains the ungated alternative.
Missing, non-approved, or unavailable plan state receives no webhook bypass and follows the
existing plan gate.

All commands below are `scripts/openswe-run` relative to this skill directory. Wakes and
results are single JSON lines; healthy monitoring is silent. Do not poll, tail, or re-check
between wakes — that is the token waste this skill exists to remove.

## Deployment

This skill deploys as a git checkout, never as copied files. Clone the repo once per
machine, delete any existing copied install and managed backup copies, then symlink
both skill directories into each surface. Do not rename or move copied skills aside:
the harnesses discover those renamed directories as additional skills. `ln -sfn` does
**not** replace an existing real directory, so deletion must happen first:

```bash
for name in openswe-run openswe-wave openswe-bundle; do
  dest=~/.claude/skills/$name
  rm -rf -- "$dest" "$dest".pre-checkout* "$dest".previous.*
  ln -s <checkout>/.claude/skills/$name "$dest"
done
```

(and the same into `${CODEX_HOME:-$HOME/.codex}/skills`). On managed operator hosts,
use `studio2-ops/bin/install-release-skills`, which performs this deletion-only
migration and verifies the run and wave symlink pairs. Install the bundle pair manually
until the managed-installer follow-up lands. The scripts resolve their
wave dependencies from the sibling skill in the checkout, so no vendoring or copying
step exists. Upgrade with `git -C <checkout> pull`; provenance is
`git -C <checkout> rev-parse HEAD`; drift is `git -C <checkout> status`.

## 0. Preflight (once)

```bash
scripts/openswe-run env
```

`rc 0` = ready. Anything missing prints a copy-pasteable `export` fix. The report includes
`langgraph_url_provenance`. A healthy explicit `LANGGRAPH_URL` wins; otherwise discovery checks
the supported Studio2 tunnels deterministically on local port 2029, then 12029. `GH_TOKEN` is
auto-derived from `gh auth token` when possible. Environment auto-derivations are recorded in
the dogfood log.

Born-ready auto-merge is repo-scoped, not global. Before the first dispatch at a repo new to
Open SWE, verify all four pieces exist, or green PRs sit CLEAN silently until the delivery
timeout (mastra-pilot PR #2): `gh api repos/<owner>/<repo>/rulesets` returns the three main
rulesets (Protect main, Review gate, Mergify automatic merge queue); `.mergify.yml` is present
on `main`; the repo setting `allow_auto_merge` is true (Mergify queues by arming GitHub
auto-merge, so with the setting off — the default on new repos — a fully green PR still sits
unqueued; mastra-pilot PR #3); and the repo is listed in the control plane's
`enabled_review_repos` store record (namespace `["enabled_review_repos"]`, key `default`).
If any piece is missing, mirror `ericlitman/open-swe`'s config — bootstrap order: Mergify
config direct-pushed to `main` first, then rulesets, then `allow_auto_merge`, then the store
opt-in — or plan for operator merges after green gates.

The pre-mint sharp edge may be retired only after both the control plane runs a release
containing the OSWE-152 fix (merge `024efcf`) and a guarded-prefix mention probe succeeds after
a provider mint, normally on a disposable issue. Until both conditions pass, or if a
stripped-grant regression recurs, perform a full-scope client-credentials mint with
`read,write,app:assignable,app:mentionable` immediately before every mention-bearing `start`,
`approve`, `reject`, `comment`, or `nudge` post. The gate passed on 2026-07-27: release
`024efcf709a34add332371dbeb5b2e395409a5ae` was deployed, and the accepted OSWE-181 23:23Z probe
posted its guarded mention without a pre-mint after a provider mint and confirmed LangGraph
handoff in run `019fa5e3-a7f3-7ff2-8e81-499daaf04464`; routine pre-mints are therefore retired.

## 1. Dispatch

```bash
scripts/openswe-run start --ticket OSWE-123 --repo owner/repo --ref main
```

For one atomic bundle, repeat `--include-ticket`; all names are resolved through Linear before dispatch, canonicalized, and checked for aliases or primary repetition:

```bash
scripts/openswe-run start --ticket OSWE-123 --include-ticket OSWE-124 --include-ticket OSWE-125 --repo owner/repo --ref main
```

The primary is the only comment/thread target. The bundle dispatch names every member, requires the agent to read and reconcile all members before one combined plan, keeps one thread-stable branch, and requires exactly one PR with a standalone `Closes <ID>` line for every member. A normalized bundle manifest is stored only in the primary dogfood log; included tickets receive no dispatch comment, thread, or product metadata. `plan`, `approve`, `reject`, `watch`, and `report` recover that manifest. A malformed manifest fails closed; absence means a legacy single-ticket run. `--force` can override each member's terminal-state guard, with one log entry per terminal member, but cannot bypass bundle placeholder, directive, duplicate, or membership checks.

Before a non-dry-run dispatch, `start` reads the workspace webhook configuration and refuses to post unless an enabled `Comment` webhook covers the primary ticket's team directly, or through `allPublicTeams: true` when the team is public. A coverage gap names the uncovered team; a read or permission failure is reported separately. The Linear credential therefore needs workspace-admin webhook read access. Bundles check the primary team because only the primary receives the dispatch comment. `--dry-run` does not perform this live coverage check.

Posts the standard dispatch comment (template embedded; see `references/run-templates.md`).
Add `--scope/--boundaries/--verify` when the ticket needs sharper rails, or `--body-file` for
a fully custom body. `--dry-run` prints the body without posting. Bodies with unfilled
`<placeholders>` are refused. Every posted body must begin with exactly one case-insensitive
`@openswe`; a repository directive may appear only immediately after that mention. A custom
`start` body must include `repo owner/name` or `repo:owner/name` there and match `--repo`
case-insensitively; missing or conflicting directives fail before posting, and matching bodies are
posted unchanged. `--force` cannot bypass these guards. Output includes `issue_id`, the derived
`thread_id`, and the confirmed LangGraph handoff. During confirmation, a parented Open SWE
`Agent Error` reply takes precedence over success or timeout and exits non-zero with its reason and
Linear comment URL. A missing confirmation within about 60 seconds otherwise exits non-zero with
baseline and final thread/run evidence. `--dry-run` does not poll.

## 2. Watch (background, exit-on-wake)

Run in a background shell; it blocks silently and exits printing **one wake JSON line**:

Use the plan deadline before approval and the delivery deadline afterward:

```bash
scripts/openswe-run watch --ticket OSWE-123 --repo owner/repo --phase plan
scripts/openswe-run watch --ticket OSWE-123 --repo owner/repo --phase delivery
```

Omitting `--phase` selects `plan`. The plan phase defaults to 30 minutes and `delivery` defaults
to 90 minutes. `--timeout-min N` explicitly overrides either default. The phase is included in
watch-start logs and `watch_timeout` evidence.

Wake nodes: `plan_posted`, `pr_opened`, `review_findings_posted`, `review_complete`,
`run_blocked`, `run_stalled`, `review_absent`, `merge_conflict`, `merge_queue_stalled`, `terminal_merged`,
`terminal_closed`, `terminal_run_error`, `unhandled_condition`, plus wrapper-level
`endpoint_failover`, `endpoint_unavailable`, and `watch_timeout` (rc 3). A successful endpoint
failover is the one
exception to exit-on-wake: it is printed immediately, and the same watch continues on the
replacement endpoint. `endpoint_unavailable` exits fail-closed without retrying the dead endpoint.
`--pr-number N` is optional; when omitted, the monitor discovers the PR from LangGraph thread metadata so PR recovery checks engage as soon as it exists.
The wrapper passes a run-stable comment watermark and `--until-wake` to `wave-monitor`, so a
child restart resumes from the last successfully classified poll instead of adopting a fresh
baseline. Control-plane failure replies, with or without a `Cause:` suffix, wake as
`terminal_run_error`; their complete reply text is retained in wake evidence. Persistent
`review_absent` and `merge_queue_stalled` conditions are watermarked by PR head across re-watches. OSWE-135 remains a known sharp edge (torn reviewThreads read → spurious
`unhandled_condition`; benign, re-watch); poll deadlines handle the OSWE-136 hung-read path.

## 3. The plan gate — adjudicate it yourself, at full weight

**A dispatched run HOLDS at plan approval. You, the caller, adjudicate — never rubber-stamp.**

On `plan_posted`:

1. `scripts/openswe-run plan --ticket OSWE-123` — read the posted plan.
2. **Now** (not earlier) read `../openswe-wave/references/adjudication-checklist.md` and apply every item
   against the ticket and the plan.
3. Write the approval body from the Approval template in `references/run-templates.md`,
   recording your challenge rulings and clarifications. For a bundle, use the Bundle approval
   reference text and name every canonical member; approval fails before the plan transition if
   any member is absent. Then:

   ```bash
   scripts/openswe-run approve --ticket OSWE-123 --body-file approval.md --adjudicated
   ```

   or reject with corrections (Reject template):

   ```bash
   scripts/openswe-run reject --ticket OSWE-123 --body-file reject.md
   ```

4. The posting command verifies or re-arms the deterministic phase watch before returning success. Approval reports a delivery watch; rejection reports a plan watch. Its JSON output includes `watch.status`, `phase`, `interval_seconds`, and `timeout_minutes`. If the replacement cannot complete a live baseline, the command exits nonzero and explicitly reports that the posted action was handed off but the ticket is unwatched.

5. Escalate to the operator only on a genuine reject-or-rework decision you cannot resolve
   from the ticket and the plan.

Repeat the phase-appropriate command until a terminal wake.

## 4. Mid-run interaction

A Linear comment on the issue lands in the running agent's mid-run queue:
`scripts/openswe-run comment --ticket OSWE-123 --body-file msg.md`. The posting commands
`approve`, `reject`, `comment`, and `nudge` use the same roughly 60-second handoff confirmation
as `start`, then verify or re-arm a background watch before returning success. Approval, comment,
and nudge use the delivery phase; rejection uses the plan phase. A matching active watch is
reported as `verified`; a replacement is reported as `rearmed`, with its interval and timeout.
The managed watch inherits each posting stream only when it is a TTY; non-TTY wake output and errors are retained in sidecar files beside the run log so piped or captured callers see EOF promptly. Pass `--repo owner/repo` when continuing a run without its original local dogfood log; otherwise the repository is recovered from the anchored dispatch record.

Stall rule: a liveness wake (`run_stalled`, threshold 30 minutes) gets **one** nudge —
`scripts/openswe-run nudge --ticket OSWE-123 --minutes 30` —
then rely on the automatically re-armed watch. A second stall wake escalates to your caller. Never loop nudges.

`review_findings_posted`: read the findings on the PR, reply/resolve via `comment` with a
`@openswe` mention; the command re-arms the delivery watch.

## 5. Report and close

On `terminal_*`:

```bash
scripts/openswe-run report --ticket OSWE-123
```

Hand back: terminal state, PR URL, merge SHA if merged, and the dogfood issues list. For a bundle, `report` performs one live `gh pr view` read for URL/state/body/merge SHA, resolves every member live from Linear, prints each standalone closing-line and terminal-state result, and returns nonzero with explicit `INCOMPLETE` for missing/failed PR evidence, a nonterminal PR, a missing closing line, an unresolved member, or a Linear state type other than `completed`/`canceled`.
**End every run by summarizing encountered issues in chat output** — say "none recorded"
explicitly if the log has no `[ISSUE]` lines.

## Dogfooding (always on)

Every command appends evidence to `<stable-root>/handoffs/<TICKET>-<date>-run.md`
(stable root defaults to `~/projects/open-swe`, override with
`OPENSWE_STABLE_ROOT`; in a Git checkout, the directory is added to the checkout-local
exclude file without changing tracked files). A retried `start` reuses the latest fragment until
that fragment contains the confirmed `[cmd] dispatched` marker; a later start after confirmed
dispatch creates a new logical-run fragment. When you hit friction — confusing output, a hang, a
wrong doc, an awkward command — record it the moment it happens:

```bash
scripts/openswe-run log --ticket OSWE-123 --issue "what happened, with the exact evidence"
```

## Sharp edges worth knowing

- Plan, blocker, and closeout selection is content-typed rather than viewer-typed. Only explicit
  `@openswe` operator actions are self-suppressed, so supported Linear actor identities may vary
  within one product workflow.
- Linear exposes no read API for the workspace app grant, so `env` cannot preflight a stripped
  `app:mentionable`; posting commands self-diagnose that failure and print the full-scope mint
  recovery.
- The live monitor needs `httpx` + `langgraph_sdk`; system python3 lacks them. The wrapper
  resolves an interpreter automatically (control-plane venv, then `uv`); override with
  `OPENSWE_RUN_PYTHON`.
- The `Open SWE Auto-fix` check conclusion is always neutral by design; the outcome lives in
  the check's `output.title`, shown as its title in review-related wake summaries.
- Exit codes: 0 wake/ok · 2 usage or environment · 3 watch timeout · 4 monitor kept dying.
