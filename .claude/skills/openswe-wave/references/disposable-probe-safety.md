# Disposable merge-gate probe safety

Use this procedure only when a verification must prove that required merge gates become
satisfied without allowing the probe commit to reach the default branch. A promise to close
the probe unmerged is a history-cleanliness requirement, not only a tree-cleanup requirement.

## Preflight: fail closed before PR creation

1. Inventory every automatic merge or queue mechanism that can target the default branch.
   Inspect repository automation files such as `.mergify.yml` and `.github/mergify.yml`, GitHub
   rulesets and branch protection, installed merge apps, and recent automatically queued PR
   evidence. A ruleset name or ID is not evidence of an app's queue predicate; inspect the
   automation configuration that contains the predicate.
2. Record the exact source, predicate, and observable suppression mechanism. For Mergify rules
   equivalent to `success_conditions: - -draft`, the suppression is an open draft PR. Do not
   infer draft exclusion from a description or from prior behavior.
3. Prove before opening the probe that every required check can complete while suppression stays
   active. For `Open SWE Review`, verify the effective `review_draft_prs` setting for the probe
   author permits draft reviews. Prior evidence is usable only when it covers the same repository,
   author, required checks, and current automation configuration.
4. Abort before creating a branch or PR when automation discovery fails, the queue predicate or
   suppression cannot be observed, a required check needs ready-for-review state, or any other
   evidence is missing. Do not open a probe with a plan to race the queue or clean up afterward.

## Run under suppression

1. Pin the default-branch SHA, create the disposable commit, and open the PR as draft. Never mark
   the probe ready for review and never arm auto-merge.
2. Query GitHub GraphQL for the current head SHA, `isDraft`, `isInMergeQueue`, `autoMergeRequest`,
   `mergeable`, `mergeStateStatus`, and required check rollup. Before waiting on checks, record
   `isDraft=true`, `isInMergeQueue=false`, and no auto-merge request. On a moved head, queue
   entry, auto-merge request, non-draft state, or unknown field, stop the proof and immediately
   close the PR. Record the deviation and terminal state; if closure loses a race with merge, the
   verification failed permanently.
3. Let every required check, including the current-head `Open SWE Review`, finish while the PR
   remains draft. Prove normal merge readiness without removing suppression: all required checks
   succeed, GitHub reports no merge conflict, and the recorded policy evidence shows draft state
   is the remaining hold. Re-read draft, queue, auto-merge, and head evidence after the last check.
4. Close the PR while it is still draft and unqueued. Re-read the terminal PR state before deleting
   the remote probe branch. Any observed merge is a failed close-unmerged verification.

## Cleanup contract

Record these results separately:

- **Suppression:** automation source, queue predicate, why it applies, draft-review capability,
  initial and final draft/queue/auto-merge evidence, and head SHA.
- **Merge-state evidence:** required check conclusions, `mergeable`, `mergeStateStatus`, conflict
  evidence, and the statement that suppression was never removed.
- **Tree cleanliness:** the default branch does not contain the probe path or content.
- **History cleanliness:** the probe commit is not an ancestor of the default branch, the PR closed
  unmerged, and the probe branch was deleted.
- **Deviation:** `none`, or the exact failed invariant and recovery action.

A merged probe fails the close-unmerged requirement permanently. Reverting or deleting its tree
content can restore tree cleanliness, but the probe and cleanup commits remain in protected
history; never report that outcome as a successful disposable verification.

## Closeout record

```text
Suppression: <automation source; predicate; applicability; draft-review evidence; PR/head evidence>
Merge-state evidence: <required checks; mergeable/conflict state; queue and auto-merge evidence>
Tree cleanup: <probe path and content absent from the default branch>
History cleanup: <PR closed unmerged; branch deleted; probe commit absent from default ancestry>
Deviation: <none, or failed invariant and recovery>
```
