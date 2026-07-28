# Project Documentation Index

Naming: evergreen specs are `kebab-case-topic.md`; point-in-time artifacts are
`YYYY-MM-DD-slug.md`. Superseded docs move to `docs/archive/` — never rewritten
in place. `INSTALLATION.md` and `CUSTOMIZATION.md` keep their upstream uppercase
names (external links from the upstream project); new evergreen docs follow the
kebab-case shape. Generated OpenWiki pages live in `openwiki/` and are not
managed under this convention.

## Setup and extension
- `docs/INSTALLATION.md` [CANONICAL] — source of truth for installing and running Open SWE.
- `docs/CUSTOMIZATION.md` [CANONICAL] — source of truth for extension points: sandbox providers, tools, middleware, graphs.
- `docs/control-plane-production.md` — macOS/Colima cutover from `langgraph dev` to the persistence-backed Agent Server.

## Point-in-time artifacts
- `docs/plans/` — Execution plans, investigation notes, comparisons, reviews. Date-prefixed. Not maintained after execution.

## Archive
- `docs/archive/` — Superseded docs. Kept for history. Never delete; never edit.
