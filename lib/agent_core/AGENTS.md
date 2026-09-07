# Agent core guidance

## Scope

This package defines execution contracts shared by the full application and
`tofu_agent`. Read `docs/modules/task_engine.md` and `docs/EVENTS.md`.

## Editing rules

- Keep run inputs, outputs, decisions, events, and lifecycle states independent
  of HTTP, persistence backends, and UI projections.
- Event names and payloads come from the canonical event contract. Producers
  emit committed facts; consumers may project them but cannot reinterpret
  settlement.
- Preserve explicit task/run/attempt identity, cancellation, terminal-state
  uniqueness, usage accounting, and typed failure attribution.
- Inject provider, tool, context, clock, and event sinks through declared ports.
  Do not read module-global user/session state.
- Keep interfaces usable by transient and durable compositions without making
  the transient runtime depend on repositories.

## Verification

Run `pytest -q tests/test_agent_core_boundary.py` plus focused run-contract and
event tests. Add task-engine integration tests when a shared lifecycle surface
changes.
