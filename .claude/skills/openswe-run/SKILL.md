---
name: openswe-run
description: Dispatch one Open SWE ticket via the @openswe Linear-comment path, watch it to a terminal state with minimal orchestration tokens, adjudicate the plan gate at checklist weight, and keep a dogfood log of process/substrate/ergonomics issues. Use for single-ticket execution; use openswe-wave for multi-ticket wave operations.
---

# openswe-run: execute one ticket

Single-run sibling of `openswe-wave`. Dispatch → watch → adjudicate plan → watch → report.
Run state changes go through an `@openswe` Linear comment (the product path — runs stay
operator-observable in the dashboard). Never create, resume, or mutate LangGraph runs via the
SDK/API; read-only status queries are what the bundled monitor already does for you.

The single exception is `approve`, which writes the plan record before it comments. A comment
alone makes the agent implement but never transitions the plan, so the product computes the
run merge-ineligible: the PR is opened as a **draft** and auto-merge is **never armed**, and
neither documented recovery covers that state. `approve` therefore performs the same plan-store
transition the dashboard's approve endpoint performs, then posts the comment — in that order,
because eligibility is resolved once, at run creation. It fails closed: if the plan record does
not come back `approved`, no approval comment is posted.

All commands below are `scripts/openswe-run` relative to this skill directory. Wakes and
results are single JSON lines; healthy monitoring is silent. Do not poll, tail, or re-check
between wakes — that is the token waste this skill exists to remove.

## 0. Preflight (once)

```bash
scripts/openswe-run env
```

`rc 0` = ready. Anything missing prints a copy-pasteable `export` fix. `LANGGRAPH_URL` is
auto-set on studio2 and `GH_TOKEN` is auto-derived from `gh auth token` when possible; both
auto-derivations are recorded in the dogfood log.

## 1. Dispatch

```bash
scripts/openswe-run start --ticket OSWE-123 --repo owner/repo --ref main
```

Posts the standard dispatch comment (template embedded; see `references/run-templates.md`).
Add `--scope/--boundaries/--verify` when the ticket needs sharper rails, or `--body-file` for
a fully custom body. `--dry-run` prints the body without posting. Bodies with unfilled
`<placeholders>` are refused. Output includes `issue_id` and the derived `thread_id`.

## 2. Watch (background, exit-on-wake)

Run in a background shell; it blocks silently and exits printing **one wake JSON line**:

```bash
scripts/openswe-run watch --ticket OSWE-123 --repo owner/repo
```

Wake nodes: `plan_posted`, `review_findings_posted`, `terminal_merged`, `terminal_closed`,
`terminal_run_error`, `unhandled_condition`, plus wrapper-level `watch_timeout` (rc 3).
After a PR exists, pass `--pr-number N` on subsequent watches so PR recovery checks engage.
Known monitor sharp edges are inherited, not re-fixed here: OSWE-135 (torn reviewThreads
read → spurious `unhandled_condition`; benign, re-watch) and OSWE-136 (hung network read;
the wrapper's heartbeat detects it, kills the monitor, surfaces the missed wake itself, and
records an `[ISSUE]` entry).

## 3. The plan gate — adjudicate it yourself, at full weight

**A dispatched run HOLDS at plan approval. You, the caller, adjudicate — never rubber-stamp.**

On `plan_posted`:

1. `scripts/openswe-run plan --ticket OSWE-123` — read the posted plan.
2. **Now** (not earlier) read `references/adjudication-checklist.md` and apply every item
   against the ticket and the plan.
3. Write the approval body from the Approval template in `references/run-templates.md`,
   recording your challenge rulings and clarifications, then:

   ```bash
   scripts/openswe-run approve --ticket OSWE-123 --body-file approval.md --adjudicated
   ```

   or reject with corrections (Reject template):

   ```bash
   scripts/openswe-run reject --ticket OSWE-123 --body-file reject.md
   ```

4. Escalate to the operator only on a genuine reject-or-rework decision you cannot resolve
   from the ticket and the plan.

Then loop back to step 2 (watch) until a terminal wake.

## 4. Mid-run interaction

A Linear comment on the issue lands in the running agent's mid-run queue:
`scripts/openswe-run comment --ticket OSWE-123 --body-file msg.md`.

Stall rule: a liveness wake (`unhandled_condition` mentioning run staleness, threshold 30
minutes) gets **one** nudge — `scripts/openswe-run nudge --ticket OSWE-123 --minutes 30` —
then re-watch. A second stall wake escalates to your caller. Never loop nudges.

`review_findings_posted`: read the findings on the PR, reply/resolve via `comment` with a
`@openswe` mention, re-watch.

## 5. Report and close

On `terminal_*`:

```bash
scripts/openswe-run report --ticket OSWE-123
```

Hand back: terminal state, PR URL, merge SHA if merged, and the dogfood issues list.
**End every run by summarizing encountered issues in chat output** — say "none recorded"
explicitly if the log has no `[ISSUE]` lines.

## Dogfooding (always on)

Every command appends evidence to `<stable-root>/handoffs/<TICKET>-<date>-run.md`
(stable root defaults to `~/projects/open-swe`, override with
`OPENSWE_STABLE_ROOT`; the directory is kept gitignored, regardless of which repo the run
targets). When you hit friction — confusing output, a hang, a wrong doc, an awkward
command — record it the moment it happens:

```bash
scripts/openswe-run log --ticket OSWE-123 --issue "what happened, with the exact evidence"
```

## Sharp edges worth knowing

- Dispatch, approval, and watch must run under the **same** `LINEAR_API_KEY` identity —
  self-suppression of your own comments is keyed to that viewer; mixing keys turns your own
  approvals into wake noise.
- The live monitor needs `httpx` + `langgraph_sdk`; system python3 lacks them. The wrapper
  resolves an interpreter automatically (control-plane venv, then `uv`); override with
  `OPENSWE_RUN_PYTHON`.
- Exit codes: 0 wake/ok · 2 usage or environment · 3 watch timeout · 4 monitor kept dying.
