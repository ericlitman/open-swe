# Wave comment templates

Replace every angle-bracket placeholder and delete unused optional lines.

## Dispatch

```markdown
@openswe repo <owner/repo> — Execute <TICKET> only.

Enter plan mode first. Re-anchor all cited paths and symbols against `<ref>`, state any refuted premise as a Challenge, and do not implement until approval is posted in this Linear thread.

Required scope: <scope>.
Boundaries: <non-goals>.
Verification: focused tests plus the repository's own lint and typecheck gates; name the exact commands in the plan.
Code standard: smallest root-cause change; no speculative validation or layered defenses; the diff must be acceptable upstream.
PR body: include the Linear reference and `Closes <TICKET>` as a standalone line. Let normal Open SWE Review and required CI run; do not directly merge or bypass gates.
```

## Bundle Dispatch

(Default body when `start` receives one or more `--include-ticket` values.)

```markdown
@openswe repo <owner/repo> — Execute one ticket bundle with primary <PRIMARY> and included tickets <INCLUDED>.

Enter plan mode first. Read and reconcile every bundle ticket before planning: <MEMBERS>. Re-anchor all cited paths and symbols against `<ref>`, state any refuted premise as a Challenge, and do not implement until the combined plan is approved in this Linear thread.

Treat the bundle as one atomic scope on the primary thread and one thread-stable branch. Included tickets must not be dispatched independently.
Required scope: <scope>.
Boundaries: <non-goals>.
Verification: focused tests plus the repository's own lint and typecheck gates; name the exact commands in the plan.
Code standard: smallest root-cause change; no speculative validation or layered defenses; the diff must be acceptable upstream.
Open or update exactly one PR for the bundle. Its body must include the Linear references and these standalone closing lines:
Closes <PRIMARY>
Closes <INCLUDED-1>
Let normal Open SWE Review and required CI run; do not directly merge or bypass gates.
```

## Bundle Approval Reference

```markdown
@openswe Combined plan approved for <PRIMARY>, <INCLUDED-1>. Proceed with the one atomic bundle only.

Challenge adjudication:
- <ratified/refused challenge and evidence across the combined scope>

Clarifications:
- <binding implementation clarification>

Run the focused tests plus the repository's own lint and typecheck gates named in the approved plan. Keep the primary thread and thread-stable branch, and open or update exactly one PR with a standalone `Closes <ID>` line for every bundle member. Let Open SWE Review and required CI run; do not directly merge or bypass gates.
```

## Approval

```markdown
@openswe Plan approved. Proceed with <TICKET> implementation only.

Challenge adjudication:
- <ratified/refused challenge and evidence>

Clarifications:
- <binding implementation clarification>

Run the focused tests plus the repository's own lint and typecheck gates named in the approved plan. Open the normal PR with the Linear reference and standalone `Closes <TICKET>`. Let Open SWE Review and required CI run; do not directly merge or bypass gates.
```

## Reject

```markdown
@openswe Plan not approved for <TICKET>. Revise the plan and repost for review — do not implement.

Blocking rulings:
- <ruling, with the evidence that refutes the plan step>

Required corrections:
- <specific change the revised plan must contain>

Scope is unchanged: <scope>. Post the revised plan in this thread and hold for approval.
```

## Nudge

```markdown
@openswe Status check on <TICKET>: no visible progress for <minutes> minutes. Post a brief status update in this thread (current step, and the blocker if you are blocked).
```

## Spot-audit

```markdown
Operator spot-audit of <PR> at `<head>`:

- Scope/file surface: <result>
- Approved plan rulings: <result>
- Acceptance invariants: <result>
- Failure and recovery paths: <result>
- Tests and unchanged boundaries: <result>

Disposition: <pass / follow-up required, with exact evidence>.
```

## Closeout

```markdown
Completed <TICKET>.

- PR and protected merge: <url> / `<merge-sha>`
- Review and CI: <result>
- Acceptance replay/live evidence: <result>
- Recovery actions, if any: <result>
- Deployment, if in scope: <result>
- Tracker: verify the Linear issue auto-transitioned on merge; flip manually only as fallback
- Follow-ups: <tickets or none>
```

## OSWE-100 tally

```markdown
Plan-gate tally — <TICKET> (<wave>)

Challenges: <count and disposition>
Questions: <count and disposition>
Unverified: <count and resolution status>

Manual adjudication catch: <what changed, or none>.
Review-layer catch: <what changed, or none; keep separate from plan challenges>.
Running ratified-challenge total: <count across plans>, with <false-count> false challenges.
```
