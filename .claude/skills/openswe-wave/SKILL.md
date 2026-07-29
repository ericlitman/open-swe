---
name: openswe-wave
description: Operate an Open SWE delivery wave with full-weight plan adjudication and low-noise mechanical monitoring. Use for wave dispatch, plan approval, spot-audit, review follow-up, closeout, recorded-event replay, the two documented merge-queue recoveries, source-anchor checks, and LangSmith trace summaries.
---

# Open SWE wave operations

## Loading contract

At invocation, load only this `SKILL.md`. Treat every file under `scripts/*` as a black-box CLI: run it and consume its output, but never read it into context. `scripts/openswe_wave.py` is an implementation detail; reading it is a defect, not diligence.

Load references only at their workflow event:

- Read `references/comment-templates.md` only when composing the relevant dispatch, approval or rejection, spot-audit, closeout, or tally comment.
- Read `references/adjudication-checklist.md` only when a plan is ready for review.
- Read `references/recovery-runbook.md` only after a stall, merge conflict, queue stall, or other recovery event.

Running `scripts/anchor-sweep` over the canonical ticket before dispatch is verification, not loading, and is required for out-of-repo/host-state tickets, tickets declaring normative sources, disposable probes, and bundles; for ordinary in-repo tickets the plan gate's re-anchor owns premise verification (OSWE-103 tally, 2026-07-28). Run the CLI without reading its implementation.

Keep plan adjudication and spot-audits at full operator weight. Use these files only to remove mechanical polling, status relay, and deterministic recovery work.
Use `openswe-bundle` first when composing a triaged batch into atomic bundles and sequenced wave siblings.

## Deployment

This skill deploys as a git checkout, never as copied files. Clone the repo once per
machine, delete any copied install and managed backup copies, then link. Do not rename
or move copies aside: the harnesses discover those renamed directories as additional
skills. `ln -sfn` will not replace a real directory, so deletion must happen first:

```bash
dest=~/.claude/skills/openswe-wave
rm -rf -- "$dest" "$dest".pre-checkout* "$dest".previous.*
ln -s <checkout>/.claude/skills/openswe-wave "$dest"
```

(and the same into `${CODEX_HOME:-$HOME/.codex}/skills`.) On managed operator hosts,
use `studio2-ops/bin/install-release-skills`, which performs this deletion-only
migration for both Open SWE skills and verifies all four links. Upgrade with
`git -C <checkout> pull` — plain `git pull` from the target repository checkout the
setup below has you working in would pull the wrong repo. Answer "what is this
machine running" with `git -C <checkout> rev-parse HEAD`; detect drift with
`git -C <checkout> status`. Do not hand-copy files into the skill directories —
copies are exactly how the installed docs went stale for a day (dogfood log,
2026-07-26).

## Required setup

Run from the target repository checkout. Live commands require the named environment variables below and fail with an export instruction when one is absent.

```bash
export GH_TOKEN=dummy
export LINEAR_API_KEY=...
export LANGGRAPH_URL=https://...
export LANGSMITH_API_KEY=...
```

`GH_TOKEN=dummy` is correct inside an Open SWE sandbox because the GitHub proxy injects the installation token. Outside that environment, set a token accepted by `gh`.

Live paths require the Python modules `httpx` and `langgraph_sdk`. The imports are lazy: `httpx` gates Linear GraphQL calls, while `langgraph_sdk` gates LangGraph thread and run reads. Fixture and replay paths (`replay --fixture`, `recover --fixture`, and `trace-digest --fixture`) and `--help` do not require either module.

On studio2, run live commands with the control-plane interpreter at `/opt/mobilyze/open-swe-control-plane/current/.venv/bin/python`. On another machine, use `uv` with `--no-project` so it does not resolve the target checkout before adding the live-path dependencies:

```bash
uv run --no-project --with httpx --with langgraph-sdk python \
  .claude/skills/openswe-wave/scripts/wave-monitor watch \
  --issue-id <linear-uuid> --repo <owner/repo> --pr-number <number> \
  --known-ids-file <persistent-path> --until-wake
```

## Plan-gated automatic merge

The working Linear-comment configuration is `auto_merge_mode=on_plan_approval` with
`require_plan_approval=true`. Approval must transition the stored plan record to `approved`
*before* posting the `@openswe` comment. The webhook reads that authoritative state and
dispatches with `plan_gate_bypass=True`, so implementation resumes instead of another plan
round. The resulting eligible default-branch PR opens **born-ready** with Mergify
reconciliation intent; Mergify owns admission and merging after required checks and Open SWE
Review pass. Manual ready-for-review recovery is not part of this path. Missing, non-approved,
or unavailable plan state receives no bypass and follows the existing plan gate.

## Workflow

1. Use `scripts/anchor-sweep <ref> <ticket-file>` before dispatch for out-of-repo/host-state tickets, tickets declaring normative sources, disposable probes, and bundles; ordinary in-repo tickets rely on the plan gate's re-anchor (OSWE-103). Treat present/moved/missing as mechanical evidence only; inspect semantic drift yourself.
   For a disposable merge-gate probe, complete `references/disposable-probe-safety.md` before creating its branch or PR.
2. Use the templates in `references/comment-templates.md` for dispatch, approval, spot-audit, closeout, and the OSWE-100 tally.
3. Apply `references/adjudication-checklist.md` before approving a plan.
4. Start the quiet monitor after dispatch:

```bash
/opt/mobilyze/open-swe-control-plane/current/.venv/bin/python \
  .claude/skills/openswe-wave/scripts/wave-monitor watch \
  --issue-id <linear-uuid> --repo <owner/repo> --pr-number <number> \
  --known-ids-file <persistent-path> --until-wake
```

The first sample suppresses historical transitions; persistent terminal, conflict, and review-absence states still wake. The only emitted wake nodes are:

- `plan_posted`
- `pr_opened`
- `review_findings_posted`
- `review_complete`
- `run_blocked`
- `run_stalled`
- `review_absent`
- `merge_conflict`
- `terminal_merged`
- `terminal_closed`
- `terminal_run_error`
- `unhandled_condition`

`pr_opened` and `review_complete` stay quiet when GitHub reports auto-merge already armed. Historical PR creation and completed reviews stay quiet on restart, while an already-conflicted PR and an open PR with neither a published Open SWE review nor an in-progress or queued current-head `Open SWE Review` check after `--review-absent-seconds` (default 900) wake immediately. Draft review-absence summaries include the ready-for-review recovery hint. Acknowledgements, normal progress, successful recoveries, queue entry/position changes, and explicit `@openswe` operator actions stay quiet. Plan, blocker, and ticket-matching terminal closeout comments remain actionable regardless of Linear author identity. `--known-ids-file` resumes comment progress across restarts, and `--until-wake` exits successfully after persisting the first emitted wake.

**Sharp edge:** the `Open SWE Auto-fix` check conclusion is always neutral by design; the outcome lives in the check's `output.title`, shown as its title in review-related wake summaries.

5. Follow `references/recovery-runbook.md`. The watch command begins before PR creation and discovers the PR from LangGraph metadata. It defaults to recovery dry-run output; after reviewing the recorded-state exercises, restart it with `--apply` to enable acting recovery.
6. Use `scripts/trace-digest <thread>` for status, token, error, recent-activity, and prompt-size rollups.
7. Complete the spot-audit and closeout templates. Confirm the tracker transition rather than assuming it.

## Status cross-check

Run this on every operator contact and at deadline boundaries to cross-check sibling progress without extending or resetting any delivery deadline:

```bash
scripts/wave-monitor status-sweep --repo owner/repo --tickets tickets.json
```

`--tickets` is one non-empty JSON list of objects with non-empty string `identifier` and `issue_id` values plus an optional non-empty string `thread_id`. Issue IDs are trimmed and lowercased; omitted thread IDs are derived from that normalized value. `--divergence-minutes` defaults to 15.

The command performs one repository-wide `gh pr list --state all` read and one LangGraph thread/plan-store snapshot per distinct `thread_id`; rows sharing a bundle thread reuse that snapshot. PR matching remains metadata-first, so bundle members can deterministically share one PR. Same-thread rows are excluded from sibling-divergence comparisons. It writes one compact, input-ordered JSON line per ticket with `identifier`, `issue_id`, `thread_id`, `lifecycle_stage`, `stage_at`, `pr_number`, `pr_state`, `thread_status`, `errors`, and `sibling_divergence`; divergence evidence names the leading sibling, stage and timestamp, elapsed lag, and threshold. Missing, malformed, ambiguous, or unavailable PR evidence leaves lifecycle stage and timestamp indeterminate and is excluded from sibling divergence rather than inferred from plan evidence. Thread metadata PR numbers are trusted when present in the repository PR list. Lifecycle precedence is `merged`, `closed`, `pr-open`, `approved`, `planned`, `dispatched`; merged and closed are terminal peers for sibling divergence. When watching a bundle, watch the primary exactly once and list every member in the status file with the primary `thread_id`; do not create another topology.

## Replay and diagnostics

```bash
scripts/wave-monitor replay --fixture tests/skills/fixtures/openswe_wave/oswe-79-events.json --max-wakes 6
scripts/wave-monitor recover --fixture tests/skills/fixtures/openswe_wave/pr-43-green-draft.json
scripts/wave-monitor recover --fixture tests/skills/fixtures/openswe_wave/pr-44-queue-stall.json
scripts/trace-digest <thread> --fixture <recorded-runs.json>
```

The monitor is disposable when OSWE-106 replaces session-side liveness polling. The templates, adjudication checklist, recovery evidence gates, anchor sweep, and trace digest remain useful operator assets. Never wire this skill into the deployed service or modify product auto-merge behavior from here.
