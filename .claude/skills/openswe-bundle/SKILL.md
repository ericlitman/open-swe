---
name: openswe-bundle
description: Compose triaged tickets into one PR, independent wave siblings, or sequenced waves, and author dispatch-ready single or bundle tickets. Use for ticket-composition decisions, triaged batch planning, wave shaping, and bundle-ticket authoring; this guidance skill hands execution to openswe-run and openswe-wave.
---

# Open SWE bundle composition

Use this procedure before dispatch. It decides what forms one delivery unit and in what order; it does not dispatch, bless, or monitor work. Hand execution to `openswe-run` for a single ticket or atomic bundle and to `openswe-wave` for independent siblings.

## Deployment

This skill deploys as a git checkout, never as copied files. Clone the repo once per machine, delete any existing copied install and managed backup copies, then symlink all three skill directories into each surface. Do not rename or move copied skills aside: the harnesses discover those renamed directories as additional skills. `ln -sfn` does **not** replace an existing real directory, so deletion must happen first:

```bash
for name in openswe-run openswe-wave openswe-bundle; do
  dest=~/.claude/skills/$name
  rm -rf -- "$dest" "$dest".pre-checkout* "$dest".previous.*
  ln -s <checkout>/.claude/skills/$name "$dest"
done
```

Repeat the loop into `${CODEX_HOME:-$HOME/.codex}/skills`. On managed operator hosts, `studio2-ops/bin/install-release-skills` currently verifies only the run and wave symlink pairs; install the bundle pair manually until the managed-installer follow-up lands. Upgrade with `git -C <checkout> pull`; provenance is `git -C <checkout> rev-parse HEAD`; drift is `git -C <checkout> status`.

## 1. Compose: choose the execution shape

For every triaged ticket, build a scope row before grouping anything:

1. Read **Source refs** and map each path plus symbol or section anchor.
2. Mark the deploy lane: skill checkout-pull, control-plane release/restart, or another independently activated boundary.
3. Mark direction risk, independent-revert needs, and the verification commands promised by the ticket.
4. Compare every pair at the file-region level, not only by title or label.

Evaluate refusal signals first. Any one blocks a bundle:

- **No cohesion** (evidence: OSWE-179): no bundle signal below exists; reducing PR count is not a reason.
- **Cross-lane membership** (evidence: OSWE-139, OSWE-178): different deploy boundaries never share a bundle.
- **Risk heterogeneity** (evidence: OSWE-149, OSWE-169): a direction ruling, likely plan-gate Challenge, or independent-revert need must not hold shippable members hostage.
- **Size** (evidence: OSWE-189): keep one combined scope adjudicable at full checklist weight, normally 2–4 members.

If no refusal applies, any one of these signals is enough to bundle. Name the chosen signal in the bundle triage comment:

- **Shared root cause**: one diff fixes several ticketed symptoms, or one member rewrites another member’s premise.
- **Same file/region**: separate PRs guarantee rebase churn or merge conflicts.
- **Atomicity**: landing only part leaves a worse intermediate state than landing none.
- **Shared review context**: each review would otherwise reconstruct the same subsystem model.

Partition everything else:

- File-disjoint tickets in the same deploy lane become `wave-sibling` assignments.
- Overlapping but uncoupled tickets become `sequenced-after(...)` assignments.
- A ticket with no safe peer remains `single`.
- Same-file tickets that are not bundled never run concurrently. The merge queue and `merge_conflict` wake are residual recovery, not a scheduling strategy.

Emit one directly usable assignment per input ticket, with the signal named after an em dash:

```text
OSWE-201: bundle(primary=OSWE-201) — shared-root-cause
OSWE-202: bundle(primary=OSWE-201) — shared-root-cause
OSWE-203: wave-sibling — file-disjoint
OSWE-204: sequenced-after(OSWE-203) — overlapping-but-uncoupled
OSWE-205: single — refusal:risk-heterogeneity
```

Record the mapping, primary choice, refusal decisions, lane boundaries, and landing order as a triage comment on each affected ticket. Session-local reasoning is not a durable wave plan. See `references/worked-examples.md` for complete analyses.

### Compose an accepted bundle

- Choose the shared-root-cause member as primary; otherwise choose the member whose scope anchors the rest.
- The primary thread owns dispatch, plan gate, thread-stable branch, PR, review, and auto-merge. Included members never receive independent dispatches while the bundle is in flight.
- Keep every member complete and independently verifiable. Each retains its own acceptance criteria, and the shared PR must satisfy all of them and carry one standalone `Closes <ID>` line per member.
- Cross-reference membership by ticket identifier only, never by URL.
- Never hand-roll membership through a custom `--body-file`; use the `--include-ticket` path so the OSWE-179 membership guards and manifest remain authoritative.

## 2. Author: make every ticket dispatch-ready

Use this body order for bundled and unbundled tickets:

1. **Context** — the observed problem and why it matters.
2. **Implementation contract** — required behavior and boundaries, not a guessed patch.
3. **Acceptance criteria** — independently testable outcomes and verification commands that exist in the target repository, phrased repo-neutrally.
4. **Non-goals** — excluded scope plus the adjacent ticket identifiers that own it.
5. **Source refs** — paths with symbol or section anchors that an agent can re-anchor against current main.

Apply these hygiene rules to all Linear prose:

- Write the dispatch mention as `[at]openswe` outside an intentional dispatch comment (evidence: OSWE-144).
- Do not put the repository-directive keyword directly before an owner/name token in prose; the webhook has matched that shape away from the dispatch prefix (evidence: OSWE-188).
- Keep repository slugs and GitHub URLs out of Linear prose; use ticket identifiers and re-anchorable source paths instead (evidence: OSWE-166).
- Wrap literal placeholder-shaped text such as `<ID>` in code spans so posting guards ignore the example (evidence: OSWE-184).

Put sequencing and timing rulings in a triage comment on the ticket itself. Include the anti-bloat standard in every implementation scope: smallest root-cause change, no speculative validation or layered defenses, and an upstream-acceptable diff.

## 3. Sequence: shape batches into waves

Apply these rules in order:

1. **Substrate dependency.** If B extends or documents what A rewrites, dispatch B only after A merges and the operator checkout is pulled: author against post-A main.
2. **Deploy boundary.** Cluster control-plane work so a wave ends at one release/restart boundary. Activation requires zero busy threads, so restart between waves, never mid-wave. Skill-side work activates on checkout pull and stays in its own lane.
3. **Adjudication bandwidth.** Stagger dispatches to the number of simultaneous plan gates the caller can adjudicate at full checklist weight; do not maximize thread count.
4. **Deadline.** Keep the normal 30-minute plan window. Start delivery at 90 minutes and add 30 minutes per included member unless ticket evidence supports another explicit window; record the ruling.
5. **Overlap.** Do not dispatch same-file, non-bundled tickets together. Merge-queue conflict recovery does not justify planned overlap.

Re-run the composition procedure after each merge that changes a later ticket’s premise. Do not re-bundle or re-sequence work already in flight.

## 4. Dispatch: hand off to the execution skills

Hand the decided unit to `openswe-run` with these exact shapes:

```bash
openswe-run start --ticket <ID>
openswe-run start --ticket <PRIMARY> --include-ticket <ID> --include-ticket <ID>
```

The installed skill exposes these through its `scripts/openswe-run` executable; `--include-ticket` is repeatable.

The bundle form posts only on the primary. Adjudicate a bundle plan once against the combined scope using `../openswe-wave/references/adjudication-checklist.md`; every member’s claims and acceptance criteria remain in scope.

For independent tickets, follow the `openswe-wave` workflow. In `status-sweep`, list every bundle member with the primary `thread_id`; watch the primary thread and shared PR once. Do not invent a second topology or dispatch included members separately.

This skill decides **what** forms a unit and **when** each unit runs. OSWE-108 owns the unresolved question of **who blesses dispatch**: batch blessing, budget envelopes, veto windows, and standing auto-dispatch policy. Do not preempt that direction ruling.

## 5. Close out: verify every member

Run the existing report on the primary:

```bash
openswe-run report --ticket <PRIMARY>
```

For a bundle, require the report to verify the shared PR, one standalone closing line per member, and every member’s live tracker state. Resolve incomplete closeout before declaring the unit done.

Every resulting PR follows the standing merge policy: auto-merge is armed, or the operator agent merges through the approved path. Never leave a green, reviewed PR waiting without an explicit blocker. Record the final state and any changed sequencing premise on the affected Linear tickets.
