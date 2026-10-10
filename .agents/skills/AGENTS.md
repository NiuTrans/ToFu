# Project skill sources

These instruction-only packages support development of this repository.
Runtime discovery belongs to `lib/skills/paths.py`; selection, bounded reads,
and ownership are specified in `docs/modules/skills.md`.

Keep each description scoped to a real task and its instructions compatible
with Tofu's available tools. Link to current domain docs instead of duplicating
their contracts. Do not assume a host-home skill directory or add install scripts
to these guides. Validate discovery and disclosure with
`tests/skills/test_skill_activation.py` when changing their integration.
