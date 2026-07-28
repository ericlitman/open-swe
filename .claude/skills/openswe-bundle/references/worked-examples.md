# Worked bundle and wave compositions

Use these as analysis patterns, not standing dispatch orders. Ticket states and source anchors must be re-read before applying an example. Signals below are the exact names expected in triage comments.

## 1. Webhook input parsing: bundle OSWE-166 with OSWE-188, sequence OSWE-144

### Scope map

- **OSWE-144** changes the Linear comment mention gate around the leading dispatch mention, leaves the exact fix shape for plan adjudication, and asks for an adjacent GitHub-path audit.
- **OSWE-166** changes Linear webhook repository extraction and visible handling of an unresolved or disallowed repository.
- **OSWE-188** narrows repository-directive parsing to the position immediately after the leading mention and adds the live mid-body regression case.

OSWE-166 and OSWE-188 describe the same directive-extraction defect at the same parser region; OSWE-188 supplies the confirmed narrower root cause while OSWE-166 retains the separate visible-rejection acceptance. OSWE-144 touches the neighboring input gate but has an uncoupled mention-parsing decision and possible audit expansion.

### Classification

Bundle OSWE-166 and OSWE-188 with **OSWE-166 as primary**. Refuse OSWE-144 from that bundle under **risk heterogeneity**, then sequence it because its neighboring input-gate work overlaps the same webhook path.

Named signals:

- **Shared root cause** — anchoring repository extraction to the leading directive position resolves the parser symptom ticketed by both OSWE-166 and OSWE-188.
- **Same file/region** — separate parser PRs would edit and test the same Linear webhook input path.
- **Risk heterogeneity refusal** — OSWE-144 still needs its mention-gate choice and adjacent audit adjudicated independently.

```text
OSWE-144: sequenced-after(OSWE-166) — overlapping-but-uncoupled
OSWE-166: bundle(primary=OSWE-166) — shared-root-cause
OSWE-188: bundle(primary=OSWE-166) — shared-root-cause
```

The shared PR must satisfy OSWE-166’s visibility criterion and both tickets’ parser regressions; OSWE-144 keeps its own plan gate and verification scope.

## 2. Auto-merge semantics: refuse one four-ticket bundle

### Scope map

- **OSWE-149** requires a direction ruling for plan-gated auto-merge eligibility across the Linear webhook, server plan mode, prompt, and PR-opening behavior.
- **OSWE-150** changes control-plane reconciliation for Mergify-owned queue state and requires live queue proof.
- **OSWE-176** changes the control-plane review gate so accepted fix-now work or an active corrective run remains merge-blocking.
- **OSWE-169** is skill/procedure work for disposable review-gate probes; its triage ruling explicitly excludes a control-plane deploy.

The titles share auto-merge vocabulary, but the root causes, rollback surfaces, and OSWE-169 activation lane differ.

### Classification

Refuse the cluster under **risk heterogeneity** and **cross-lane membership**. OSWE-149 has unresolved design options, OSWE-150 changes queue reconciliation, OSWE-176 changes a required merge gate, and OSWE-169 activates by skill checkout rather than control-plane release. Shared vocabulary is not cohesion, and no unsupported substrate dependency should be invented between the product tickets.

OSWE-150 and OSWE-176 may be parallel control-plane siblings only after source mapping confirms their files are disjoint and the caller can adjudicate both plans. Keep OSWE-149 and OSWE-169 independent.

```text
OSWE-149: single — refusal:risk-heterogeneity
OSWE-150: wave-sibling — file-disjoint
OSWE-176: wave-sibling — file-disjoint
OSWE-169: single — refusal:cross-lane-membership
```

The control-plane wave ends at one release/restart boundary. OSWE-169 ships in the separate checkout-pull lane and retains independent closeout evidence.

## 3. Installation resolution: refuse OSWE-139 with OSWE-178

### Scope map

- **OSWE-139** changes and versions host-side sandbox shims, with installation into the operator control-plane bin directory and live host verification across repositories.
- **OSWE-178** changes product error semantics in the authentication and GitHub App installation-resolution path for a transferred or stale repository slug.

Both discuss App installation lookup, so they look bundleable by subsystem name. Their actual delivery lanes differ: OSWE-139 owns host/operations artifacts and install activation, while OSWE-178 owns control-plane product code and release/restart activation.

### Classification

Refuse under **cross-lane membership**. A shared PR cannot represent both repositories or both activation boundaries, and either change may need to ship or revert without the other. Keep separate closeout evidence for the host shim smoke and the product error tests.

```text
OSWE-139: single — refusal:cross-lane-membership
OSWE-178: single — refusal:cross-lane-membership
```

They may be planned in adjacent waves, but the control-plane release and host install remain separate boundaries.

## 4. Mechanics before guidance: sequence OSWE-189 after OSWE-179

### Scope map

- **OSWE-179** creates the bundle mechanics: included-ticket flags, manifest recovery, combined-scope approval, shared-thread wave status, and per-member report checks.
- **OSWE-189** documents composition judgment and hands execution to those mechanics.

### Classification

Do not bundle them. OSWE-179 was already in flight with scope frozen at its plan gate, and mechanics versus guidance are different deliverable classes. More importantly, OSWE-189’s commands and closeout procedure are invalid until OSWE-179 exists on main and the operator checkout is pulled.

Named signals:

- **Risk heterogeneity refusal** — adding a new judgment document to an in-flight mechanics change would hold the mechanics delivery to a second review scope.
- **Substrate dependency** — OSWE-189 must be authored against post-OSWE-179 main.

```text
OSWE-179: single — refusal:risk-heterogeneity
OSWE-189: sequenced-after(OSWE-179) — substrate-dependency
```

This is the same landing-order method used when predecessor wave work rewrote the run report and dispatch templates before OSWE-179: merge the substrate wave, pull the operator checkout, then author the extension against the landed contract. Same-file work in the intervening wave may run only where its regions are genuinely disjoint; otherwise sequence it rather than relying on the `merge_conflict` wake.
