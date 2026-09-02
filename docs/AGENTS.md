# Documentation guidance

## Scope

This file applies to `docs/`. The root `AGENTS.md` still applies. Documentation
describes the system that runs now; Git history is the archive.

## Start here

- `README.md` is the first-hop map.
- `ARCHITECTURE.md` owns process, layer, and dependency boundaries.
- The relevant file in `modules/` maps a domain to code and focused tests.
- Machine-readable contracts and their generated consumers override prose
  examples.

## Editing rules

- Every Markdown file under this directory, including this file, must appear
  exactly once in `catalog.json` and stay within its group's line budget.
- Update an authority document in the same change as the behavior or contract
  it describes. Reference guides may explain an authority but may not redefine
  it.
- Keep only current invariants, operations, and rationale. Remove completed
  plans, incident narratives, temporary audits, and superseded alternatives.
- Prefer links to canonical owners over copied schemas, field lists, commands,
  or inventories. Generated documents name their generator and are never
  hand-edited.
- Keep local links repository-relative and valid. Do not add secrets, private
  runtime data, or conversation transcripts.

## Verification

Run `make docs-check`. When architecture ownership changes, also run
`python3 scripts/check_architecture.py` and the focused contract tests named by
the affected domain map.
