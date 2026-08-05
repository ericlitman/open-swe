# Reviewer dispatch concurrency — accepted-risk decision (OSWE-257)

**Date:** 2026-08-05 · **Repository state reviewed:** `123f6c3f` (post-OSWE-258) ·
**Status:** accepted risk; runtime acceptance criteria waived

## Decision

Do not add dispatch coordination for the residual reviewer races owned by OSWE-257. The ticket's
runtime ordering, lifecycle, and dispatch-boundary regression criteria are formally and totally
waived. No partial guard, lock, sequencing state, or handler-specific mitigation is being shipped.

The exposure is real, but production evidence reported on the ticket found zero occurrences during
roughly twenty-four hours of hostile dogfooding across five pull requests. The in-process windows
are sub-second and bounded by awaited network calls, delayed or redelivered older push webhooks are
rare, and a review retrigger is a verified recovery. That risk does not justify a new durable
dispatch subsystem with broader correctness and operational failure modes.

## Residual failure modes

At the reviewed commit, automatic first review and watched-push review both write reviewer-thread
metadata, create an `Open SWE Review` check, and create an interrupting reviewer run:

- `_dispatch_first_review_from_pr_payload` in `agent/webhooks/github.py` confirms the recorded head,
  watch state, and check ownership before check creation and dispatch. A newer push can complete its
  own dispatch after that confirmation but before the older path creates its check and run; the
  older arrival can then replace check ownership and interrupt the newer run.
- `process_github_push_event` in `agent/webhooks/github.py` has the mirror-image gap after recording
  the pushed head and before dispatch. Concurrent producers therefore still resolve by run-creation
  arrival, not by head freshness.
- A close or ineligible draft transition can land after first-review refresh or confirmation. A
  later stale watch write can overwrite the lifecycle update, or dispatch can follow the update
  from a stale snapshot.
- Since OSWE-258 correctly made the push payload's `after` SHA authoritative, a delayed or
  redelivered webhook for an older push can record the head backward, create a stale-commit check,
  and interrupt the current-head review.

OSWE-252 narrows the first-review window with refresh and pre-dispatch stand-down. OSWE-251's
reviewed-head dedupe and run-scoped check ownership, OSWE-255's per-dispatch event classification,
and OSWE-258's authoritative pushed-head behavior remain unchanged.

## Rejected coordination designs

### Process-local per-thread lock

A process-local lock cannot coordinate separate webhook workers and cannot cover delayed or
redelivered deliveries at all. Holding it across the awaited metadata, GitHub check, and run
creation calls would also introduce head-of-line blocking and lock-lifecycle failure modes for a
race not observed to occur.

### Thread-metadata monotonic guard

Reviewer thread metadata updates are last-write-wins. The LangGraph thread metadata API has no
compare-and-set operation, so an atomic monotonic-head read/modify/write guard cannot be built on
that state. Another read followed by another write only moves the same race.

### Post-dispatch self-verification

This does not satisfy the ordering guarantee. `multitask_strategy="interrupt"` takes effect when the
stale run is created, so the stale dispatcher has already interrupted the newer run before it can
re-read metadata and cancel itself. Cancellation cannot restore the victim. Re-dispatching the
interrupted newer run would be resurrection machinery with its own ownership, duplication, and
failure semantics, not a guard.

### Durable dispatch arbiter or queue

A durable per-PR decision point could serialize producers, but making it complete would require a
new subsystem that receives every first-review, push, and lifecycle event; validates authoritative
PR eligibility and head state; owns check creation; and launches or restores reviewer runs. That is
materially larger than the observed and recoverable exposure. A partial arbiter would leave one of
the ticket's three vectors open and is therefore explicitly rejected.

## Operational detection and recovery

### Stale-head ordering vectors

The characteristic symptom is the current PR head wearing an incomplete or failed
`Open SWE Review` check while a review was published against an older head. This typically
appears as a blocked merge with all unrelated required checks green. Correlating the reviewer
thread metadata, check commit, and published review commit should show the head/check ownership
moving backward or an older dispatch arriving after the current-head dispatch.

Recovery is to retrigger review for the current pull-request head. The current reviewed-head,
check-ownership, and per-dispatch classification behavior then establishes a fresh current-head
check and review.

### Lifecycle vector

The characteristic symptom is a closed or ineligible-draft pull request whose reviewer thread has
`watch=true`, possibly with a review or check created after the lifecycle transition. This does not
present as an older-head review and must not be recovered by retriggering review, which would launch
another unwanted review and keep watching enabled.

Recovery is to restore the intended lifecycle state by clearing the reviewer thread's watch flag.
Treat any review or check produced after the close or ineligible-draft transition as noise on a pull
request that should not have been reviewed.

Reopen this decision if any of the following occurs:

- one confirmed production occurrence of either signature;
- evidence that its frequency or recovery cost is rising; or
- the platform gains an appropriate ordering primitive, such as compare-and-set for thread
  metadata, that can enforce monotonic ownership without a new arbiter subsystem.

## Formal waiver

For OSWE-257, the following acceptance criteria are waived rather than implemented:

- superseded-head dispatches are not guaranteed to avoid interrupting, replacing, or taking check
  ownership from newer-head dispatches;
- close or draft transitions in the final startup gap are not guaranteed to prevent a subsequent
  stale-snapshot dispatch; and
- no new dispatch-boundary regression tests are added for those waived behaviors.

The OSWE-251 reviewed-head/check-ownership guarantees and OSWE-255 per-dispatch classification
remain in force and are intentionally untouched. This record is the complete OSWE-257 deliverable.
