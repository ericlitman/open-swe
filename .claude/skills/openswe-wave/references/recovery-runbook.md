# Merge-queue recovery runbook

Both procedures default to dry-run. Review the emitted evidence and commands before adding `--apply`. Apply mode re-reads every gate and the head SHA immediately before mutation. Any ambiguity becomes `unhandled_condition` and performs no action.

## Green, CLEAN, draft

Trigger only from LangGraph metadata `auto_merge_alert_reason=green_draft`.

Required evidence:

- PR is open, draft, targets the repository default branch, and has a stable head SHA.
- Required checks are green and merge state is CLEAN.
- GitHub GraphQL reports `isInMergeQueue=false`.
- The merge-hold label is absent.
- At least one `ConvertToDraftEvent` exists.
- Every event actor is GraphQL `__typename=Bot` and `login=mobilyze-open-swe-studio2`.
- No Linear action marker exists for the same reason, PR, and head.

Dry-run:

```bash
scripts/wave-monitor recover --issue-id <issue> --thread-id <thread>   --repo <owner/repo> --pr-number <number>
```

Apply adds `--apply` and executes, in order:

```bash
gh pr ready <number> --repo <owner/repo>
gh pr merge <number> --repo <owner/repo> --auto --squash --match-head-commit <head-sha>
```

The monitor verifies the unchanged head is non-draft with auto-merge armed, then posts a deduplicated Linear action log. A human, missing, null, or unknown actor blocks recovery.

## Armed, green, CLEAN, unqueued

This is a regression fallback. Product reconciliation owns the normal one-shot recovery.

Trigger only from LangGraph metadata `auto_merge_alert_reason=queue_stall`.

Required evidence:

- PR is open, non-draft, targets the default branch, and has a stable head SHA.
- Auto-merge is armed, required checks are green, and merge state is CLEAN.
- GitHub GraphQL reports `isInMergeQueue=false`; never substitute `gh pr view --json`.
- The merge-hold label is absent.
- No Linear action marker exists for the same reason, PR, and head.

Apply mode executes, in order:

```bash
gh pr merge <number> --repo <owner/repo> --disable-auto --match-head-commit <head-sha>
gh pr merge <number> --repo <owner/repo> --auto --squash --match-head-commit <head-sha>
```

The monitor verifies the unchanged head remains non-draft with auto-merge armed and posts the Linear action log. It never directly merges or bypasses checks.


## Armed, green, stalled in Mergify queue

Trigger only from LangGraph metadata `auto_merge_alert_reason=queue_stall_in_queue`. The monitor emits `merge_queue_stalled`; recovery is operator-only because refresh and requeue did not clear the recorded stall.

The known cause is a Mergify branch update producing a new PR head without a required `Open SWE Review` check. Confirm the PR is open, auto-merge remains armed, the reported head is current, required CI is otherwise green, and `main` is quiet. Then retrigger review on that updated head:

```bash
gh pr ready <number> --repo <owner/repo> --undo
gh pr ready <number> --repo <owner/repo>
```

Wait for a fresh `Open SWE Review` check on the unchanged head and let normal queue protections converge. If `main` advances again, the queue may update the head and repeat the condition; do not loop the toggle on an active repository. This procedure is historical fallback once OSWE-246 is deployed, because that change makes review checks materialize on queue branch updates.

## Stand down

Do not mutate when any evidence gate fails, the head moves, an API call fails, a prior action marker exists, the PR is terminal, or post-action verification fails. Emit `unhandled_condition` with the blocking evidence for operator judgment.
