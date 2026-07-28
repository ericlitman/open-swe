# Plan-gate v2 adjudication retro — tally and recorded decision (OSWE-103)

Date: 2026-07-28. These are the batch retro notes required by OSWE-103 (carried out
of OSWE-100's third acceptance bullet). Plan gate v2 — `### Challenge` (conditional),
`### Unverified claims` (always), `### Questions` (conditional) — went live on studio2
with release `2ea9dcb2` on 2026-07-22. This retro tallies every plan-gated dispatch
from that activation through the 2026-07-28 morning canary (OSWE-134 / PR #101)
against the manual adjudication that happened anyway, and records the decision the
tally supports.

## Method

- Population: every dispatch that produced a plan-gate run — OSWE-86 wave 0 and all
  subsequent dispatches across `open-swe`, `studio2-ops`, `threadbear` — 71 plan
  units covering 74 tickets (bundles OSWE-144+166, OSWE-170+143, OSWE-141+147 count
  once each). Sources: Linear comment streams (plans, approvals, tally comments),
  `handoffs/` dogfood run logs, `WAVE-U1U2` wave log.
- **Challenges** / **Questions** / **Unverified** columns give the section's item
  count in the posted plan (`—` = section absent and not required, `✗` = section
  absent though mandatory, `n/r` = plan lived in the dashboard plan record, sections
  not reproducible from the Linear thread, counts taken from the approval text).
- **False challenge**: any challenge (or challenge premise) the adjudicator ruled
  wrong.
- **Manual catch**: something manual adjudication caught that the gate's output did
  not surface. `ruling` = adjudicator ruled a gate-surfaced Question (the intended
  division of labor, listed for completeness); `minor`/`decisive` = correction the
  gate did not surface at all.

Excluded (no plan-gate run): OSWE-92 (pre-v2 — plan posted by operator request
before activation), OSWE-100 (the run that shipped v2), OSWE-98 and OSWE-109
(direct no-plan dispatches), OSWE-140 (manual build), OSWE-187 (umbrella),
OSWE-188 (closed by the OSWE-144 bundle's PR), BEAR-40 (direct dispatch),
BEAR-42 (canceled undispatched), BEARWEB-10 (dispatch silently dropped —
non-enrolled team, see OSWE-197; executed operator-side), OSWE-198 (disposable
webhook probe), OSWE-91/127/128/129 and OSWE-190–197 (not yet dispatched).

## Tally

| Unit | Plan | Ch | Q | UV | False challenge | Manual catch beyond the gate |
|---|---|---|---|---|---|---|
| OSWE-85 | 07-22 | — | — | 1 | — | minor: two unstated requirements added (no-profile path must work; persona carried verbatim) |
| OSWE-87 | 07-22 | — | — | 3 | — | **decisive: required change — new `REVIEWER_ROUTING_DEFAULT` env seam; reusing `REVIEWER_ASSISTANT_ID` would couple an eval knob to production routing** |
| OSWE-82 | 07-22 | 1 | — | 3 | — | — (stale deployed-release id in an Unverified claim corrected on the record) |
| OSWE-83 | 07-22 | 1 | 2 | 0 | — | ruling ×2; cross-lane edit guardrails added |
| OSWE-84 | 07-22 | — | — | 2 | — | — |
| OSWE-99 | 07-22 | — | ✓ | ✓ | — | ruling |
| OSWE-94 | 07-22 | 1 | — | ✓ | — | — |
| OSWE-88 | 07-23 | — | — | 2 | — | — |
| OSWE-79 | 07-23 | — | 2 | 3 | — | ruling: Q1 answer **refused** the plan's preferred reading (per-graph "single home"); ticket AC corrected |
| OSWE-89 | 07-23 | — | — | 2 | — | — |
| OSWE-90 | 07-23 | — | — | 3 | — | — |
| OSWE-122 | 07-23 | 2 | — | ✓ | — | — (gate caught the dispatch's own 46-min-stale ref) |
| OSWE-123 | 07-23 | 1 | ✓ | ✓ | — | ruling |
| OSWE-112 | 07-23 | 1 | — | ✓ | — | — (approved; ticket later cancelled as superseded) |
| BEAR-5 | 07-24 | 2 | 1 | 2 | — | ruling: operator-side handoff facts supplied (branch/PR conventions) |
| OSWE-138 | 07-25 | 1 | — | ✓ | **partial: challenge premise "LangSmith proxy is the auth boundary" refuted — deployment runs `SANDBOX_TYPE=local`, host shims authenticate** | **decisive: fix redirected to the host-shim path; `GITHUB_APP_TARGET_REPO` env hook designed at adjudication** |
| OSWE-142 | 07-26 | — | — | 3 | — | **decisive: binding corrections — `uv run --no-project` fallback, venv-not-version framing, shebang risk elevated to a deliverable** |
| OSWE-136 | 07-26 | 2 | 1 | 2 | — | ruling; minor: page-cap sizing bound added |
| OSWE-145 | 07-26 | 1 | — | 3 | — | — (all three Unverified claims ratified with operator-side evidence) |
| BEAR-41 | 07-26 | — | — | 3 | — | — (premises pre-verified by operator audit before dispatch) |
| BEAR-27 | 07-26 | 3 | — | 1 | — | minor: repair-failure semantics bound to the `classifier_unavailable` precedent; downgrade-to-older-binary requirement added |
| OSWE-125 | 07-26 | — | 2 | 3 | — | minor: unsatisfiable in-sandbox pre-work gate re-homed to provisioning time; legacy interim chosen |
| OSWE-126 | 07-26 | — | — | 3 | — | — (wave tally comment: "Manual adjudication catch: none") |
| OSWE-153 | 07-27 | 1 | ✓ | ✓ | — | ruling; challenge ratified as provisional |
| OSWE-146 | 07-27 | 1 | — | 2 | — | minor: never-print-nothing fallback + dispatch-detection constraints added (review later caught the concrete boundary bug in this class) |
| OSWE-154 | 07-27 | 1 | — | 2 | — | minor: ratified challenge extended to the sibling skill's wake-node list |
| OSWE-167 | 07-27 | 5 | — | n/r | — | — (operator issued a mid-plan ticket-scope correction; challenge 3 commended — busy-baseline false-success hole) |
| OSWE-157 | 07-27 | — | — | 2 | — | — |
| OSWE-135 | 07-27 | — | — | 3 | — | — |
| OSWE-168 | 07-27 | 1 | — | 3 | — | — (challenge commended: stdout-buffering root cause **superseded the operator's own hang diagnosis**) |
| OSWE-162 | 07-27 | — | — | 1 | — | — |
| OSWE-161 | 07-27 | — | — | 2 | — | — |
| OSWE-156 | 07-27 | — | — | 2 | — | — |
| OSWE-137 | 07-27 | 1 | — | 3 | justification only | minor: gate's "ticket explicitly requires" justification corrected (scope kept on other grounds) |
| OSWE-148 | 07-27 | 1 | — | 2 | — | — |
| OSWE-173 | 07-27 | 1 | — | 3 | — | — (review-layer catches handled at adjudication weight post-PR) |
| OSWE-131 | 07-27 | 1 | — | 4 | — | — |
| OSWE-172 | 07-27 | 1 | — | 0 | — | — |
| OSWE-152 | 07-27 | — | — | 3 | — | — (first Unverified claim confirmed operator-side: live grant outage) |
| BEAR-61 | 07-27 | 1 | — | 1 | — | — |
| BEAR-62 | 07-27 | — | — | 1 | — | — (macOS smoke from Unverified claims assigned to operator spot-audit) |
| OSWE-181 | 07-27 | 2 | — | 2 | — | — (adjudicator supplied dated host evidence for both ratifications) |
| OSWE-184 | 07-28 | — | — | 2 | — | — (premise pre-confirmed by adjudicator's live reproduction) |
| OSWE-185 | 07-28 | — | — | 1 | — | — |
| OSWE-183 | 07-28 | — | — | 2 | — | — (missing-fields premise reproduced live by adjudicator) |
| OSWE-182 | 07-28 | — | — | 4 | — | **decisive: binding correction — recovery text must not swap the minted app token into `LINEAR_API_KEY` (identity mixing / webhook self-suppression)** |
| OSWE-180 | 07-28 | — | — | 2 | — | — (adjudicator added stronger live re-fire evidence) |
| OSWE-186 | 07-28 | — | — | 2 | — | — |
| OSWE-179 | 07-28 | 1 | — | 2 | — | — |
| BEAR-57 | 07-28 | 1 | — | 2 | — | — |
| OSWE-189 | 07-28 | — | — | 1 | — | — |
| BEAR-55 | 07-28 | — | — | 3 | — | — (upstream reading verified on operator host before approval) |
| BEAR-63 | 07-28 | — | — | 4 | — | — |
| OSWE-171 | 07-28 | — | 1 | 3 | — | ruling: endpoint preference order + failover-wake semantics |
| OSWE-141+147 | 07-28 | 5 | — | n/r | — | — |
| OSWE-170+143 | 07-28 | — | — | 2 | — | — (plan's scope reconciliation ratified; review later caught closeout-classifier gap) |
| OSWE-169 | 07-28 | 1 | — | 4 | — | — |
| OSWE-151 | 07-28 | 1 | 1 | 3 | — | ruling: still-progressing exits non-zero, rollback command suppressed |
| OSWE-139 | 07-28 | 1 | — | 4 | — | — |
| BEAR-64 | 07-28 | 1 | — | 2 | — | — |
| BEAR-65 | 07-28 | 1 | — | ✗ | — | — (resumed run posted a nonstandard plan — no `## Plan` heading, no Unverified section; broke `plan_posted` detection → redundant re-dispatch, approve initially failed closed) |
| OSWE-144+166 | 07-28 | 1 | — | ✗ | — | — (bundle plan omitted the mandatory Unverified section) |
| OSWE-165 | 07-28 | 1 | — | 4 | — | — |
| OSWE-150 | 07-28 | 1 | — | 3 | — | — |
| OSWE-176 | 07-28 | — | — | 4 | — | — |
| OSWE-178 | 07-28 | 1 | — | 3 | — | — |
| OSWE-149 | 07-28 | 1 | — | 2 | — | minor: three-option design choice sat in plan prose rather than a gate Question; adjudicator ruled it at full weight |
| BEAR-44 | 07-28 | 1 | — | 2 | — | — |
| BEAR-58 | 07-28 | — | — | 2 | — | — |
| BEAR-34 | 07-28 | 1 | 1 | 4 | — | ruling: corrected prerelease model accepted |
| OSWE-134 | 07-28 | 1 | — | 2 | — | — (canary for the plan-approved born-ready path) |

## Aggregates

- **71 plan units; zero plans rejected.** Every plan reached approval, several after
  corrections folded into the approval. The only approve failures were mechanical
  (BEAR-65's `shared` plan record → OSWE-193; approval-pickup/webhook-ordering
  hiccups on OSWE-146/149/179/181/BEAR-57 — all recovered without content changes).
- **Challenges: 53 individual challenges across 39 of 71 plans (55%).**
  52 ratified outright — many corrected premises the operator's own triage had
  missed or introduced (stale refs, superseded wording, transferred-repo slugs,
  dispatch-template boilerplate), and two were commended for superseding the
  operator's own diagnosis (OSWE-168 stdout buffering, OSWE-167 busy-baseline
  hole). **One challenge premise was refuted** (OSWE-138: misread the deployment's
  auth boundary from in-repo evidence — a host-state fact). One ratified challenge
  carried a wrong justification (OSWE-137). **Zero fully-false challenges.**
- **Questions: 11 of 71 plans asked Questions; all required and received operator
  rulings.** None were answerable from inside the sandbox (operator intent, host
  facts, policy).
- **Unverified claims: emitted in 67 of 69 reproducible plans.** Consistently
  honest; several required operator-side resolution to unblock or bind (OSWE-125
  provisioning gate, OSWE-142 interpreter facts, OSWE-152 live outage, BEAR-62
  smoke). One stale factual claim corrected on the record (OSWE-82 deployed id).
  **Two mandatory-section omissions** (OSWE-144+166 bundle, BEAR-65 resumed run)
  — both on bundle/resume paths; BEAR-65's nonstandard format also broke monitor
  `plan_posted` detection and caused a redundant re-dispatch.
- **Manual adjudication caught something the gate did not surface in 12 of 71 units
  — 4 decisive (OSWE-87, OSWE-138, OSWE-142, OSWE-182), 8 minor.** All four
  decisive catches, and the single refuted challenge premise, are host-state /
  deployment-environment / operator-policy facts — exactly the tier the OSWE-100
  eval predicted is structurally invisible to the sandbox gate. **Zero decisive
  manual catches occurred on purely in-repo tickets.**
- The gate does not subsume the review layer: Open SWE Review and operator
  spot-audits caught real post-approval defects (OSWE-146 boundary bug, OSWE-170
  closeout classifier, OSWE-138 delta-pass analyzer ordering, BEAR-5 umask replay).

## Recorded decision

**Manual pre-dispatch adjudication narrows. Plan-review adjudication at the gate
continues at full checklist weight.**

1. **Stands down** for ordinary in-repo tickets: operator-side pre-dispatch
   re-verification of in-repo anchors and premises (anchor sweep + opening every
   cited path). The gate's re-anchor + Challenge output now owns that verification:
   across 39 challenge-bearing plans it caught every stale in-repo premise that
   reached it, with zero fully-false challenges, including staleness the operator
   side had introduced. Anchor sweep remains a ticket-authoring aid at writing time.
2. **Remains required** — full pre-dispatch verification — for the narrowed tier:
   out-of-repo/host-state tickets (all 4 decisive catches + the one refuted
   challenge premise live here), tickets declaring normative external sources,
   disposable probes (per `disposable-probe-safety.md`), and bundles
   (combined-scope reconciliation).
3. **Does not narrow**: plan-review adjudication of the gate's output. Questions
   require operator rulings by construction; Unverified claims routinely need
   operator-side resolution to unblock or bind; prose-buried design choices still
   need full-weight rulings (OSWE-149); and mandatory-section omissions (2/71) are
   only noticed by a human reading the plan. Rubber-stamping stays prohibited
   (OSWE-140 scope correction stands).

The operative change lands alongside this retro in the wave skill's operator
surfaces: the adjudication checklist's "Before dispatch" section and the two
`SKILL.md` sites (loading contract, workflow step 1) that previously mandated
anchor-sweep before every dispatch.

## Follow-ups

- Gate-contract defect filed as OSWE-199: resumed and bundled plan runs can omit
  the mandatory `### Unverified claims` section (OSWE-144+166, BEAR-65), and
  BEAR-65's resumed run also dropped the `## Plan` heading — breaking monitor plan
  detection. Related but distinct: OSWE-193 (plan-record state loss on extra
  runs), OSWE-196 (monitor detection keying on heading shape).
- OSWE-91 (wave 5 routing flip) remains undispatched; its plan run will be the
  first test of the narrowed procedure on a deployment-heavy ticket — it falls in
  the remains-required tier.

## Addendum — validity caveats and decision status (2026-07-28, post-merge)

Recorded after operator review of this retro on the OSWE-103 thread. This
section is append-only; the body above is unchanged. It corrects how the body
should be read and changes the decision's status.

**Roles.** "Manual adjudication," "operator," and "adjudicator" above mean the
orchestrating Claude session, not a human. That session performs most ticket
writing, the pre-dispatch check, plan review, and the post-implementation
spot-audit (auditing the diff against the approved plan before the independent
reviewer and CI settle — a stage the body's framing underplays). The human
operator appears for host-only actions and as final authority. The pipeline
therefore has the same session class at four of six stages, evaluating a gate
whose inputs it prepared.

**What the tally did not measure.** The "Manual catch" column counts only
catches made at plan review. Everything the session caught *before* dispatch
became an invisible ticket edit — corrected descriptions, staleness notes,
pre-dispatch audits — and was never scored as a catch. The measured tickets
were also unusually clean at authoring: most were written by sessions reading
the code minutes earlier. "Zero decisive in-repo catches" therefore partly
reflects pre-dispatch curation doing its job, not proof that the layer was
redundant. There is additionally a self-grading limitation: the same session
class adjudicated the plans and scored those adjudications here; the only
fully independent judges in the loop are the reviewer graph, CI, and the human
operator.

**What still stands.** The host-state boundary is structural, not a curation
artifact: all four decisive plan-review catches and the single refuted
challenge premise concern facts the sandbox cannot observe. And the 53
challenged premises are a genuine sample of curation failure — each reached
the gate only because the session's pre-dispatch pass missed it or the code
moved after triage — so "the gate reliably catches in-repo staleness that
slips past curation" is supported (52/53 handled correctly, none fully false).

**Decision status: downgraded from conclusion to monitored hypothesis.** The
narrowed before-dispatch rule stays in force for the next batch, which
supplies the missing sample (less-curated tickets reaching the gate). During
that batch, every pre-dispatch ticket correction and every in-repo defect
first caught at plan review, spot-audit, code review, or CI is recorded as a
data row; the re-tally and keep-or-revert ruling are tracked as OSWE-200.
Revert restores the prior always-verify wording in the adjudication checklist
and both wave `SKILL.md` sites.
