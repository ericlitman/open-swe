# Wave adjudication checklist

## Before dispatch

- Pin the current target ref and run `scripts/anchor-sweep` over the canonical ticket text.
- Open every cited path and symbol; moved anchors require semantic re-verification.
- Any source declared normative (for example, adopt verbatim, match exactly, or byte-identical) must resolve from the target repository at the pinned ref; otherwise inline the exact content in the ticket or dispatch and identify the substitution explicitly.
- Verify live-state claims with read-only GitHub, Linear, or LangGraph queries.
- Separate deployed/runtime claims that cannot be checked into Unverified claims.
- Reconcile predecessor tickets and recently merged work before preserving old scope.
- Confirm the requested mechanism is not already owned by product code or an existing operator tool.

## Plan review

- Challenges quote exact contradicting evidence, state what would disprove it, and correct scope visibly.
- Questions capture operator intent rather than silently deciding it.
- Unverified claims are honest external dependencies, not narrated successful checks.
- Every acceptance criterion maps to a deliverable and a focused verification step.
- For a bundle, re-anchor and reconcile the combined scope across every member before approving one atomic plan.
- The plan names concrete verification commands that exist in the target repository.
- New state, config, dependencies, or subsystems name the simpler rejected alternative.
- Challenge unnecessary complexity, machinery layered on machinery, out-of-contract edge-case defenses, and validation of impossible states; prefer the root-cause fix that makes those layers moot.
- Failure behavior is fail-closed where mutation, merge, credentials, or incomplete evidence is involved.
- Product and operator boundaries remain explicit.
- The PR title/body, release note, test plan, closing line, and Linear reference follow repository conventions.

## Spot-audit

- Compare the diff to the approved plan, including refused scope.
- Inspect actor/identity evidence, stale-head protection, and mutation command order.
- Confirm quiet-path behavior does not wake on baselines, acknowledgements, progress, or queue movement.
- Confirm every approved wake node is reachable and no extra node is emitted.
- Confirm required checks and review gates cannot be bypassed.
- Confirm fixtures distinguish observed facts from inferred transient fields.
- Run the focused tests independently and inspect representative CLI output.
- Treat bloat as a finding even when scope matches and checks are green: would the upstream maintainer accept the diff as a minimal patch?
