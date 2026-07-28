# openswe-run comment templates

Replace every angle-bracket placeholder and delete unused optional lines. `openswe-run`
refuses bodies with unfilled placeholders in prose. Contents of complete inline backtick spans
and fenced code blocks are ignored; `--force` remains available for intentional bare placeholders.
Dispatch and Approval are verbatim-compatible with the openswe-wave templates — do not drift them
independently.

## Dispatch

(Default body of `openswe-run start`; shown for `--body-file` customization.)

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

Record real adjudication rulings — the checklist must have been applied first
(`approve` refuses without `--adjudicated`).

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

(Default body of `openswe-run nudge`. One nudge per stall, ever — then escalate.)

```markdown
@openswe Status check on <TICKET>: no visible progress for <minutes> minutes. Post a brief status update in this thread (current step, and the blocker if you are blocked).
```

## Review-findings reply

```markdown
@openswe Review findings on <PR> acknowledged for <TICKET>.

- <finding>: <fix now / justified as-is, with evidence>

Address the accepted findings, push to the same branch, and let Review and CI re-run.
```
