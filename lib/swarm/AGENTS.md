# Multi-agent coordination guidance

## Scope

This package coordinates parent/child agent work on top of the canonical task
engine. It is not a second model loop, event taxonomy, persistence authority, or
project board.

## Editing rules

- Carry root task, parent/child, owner, role, objective, and budget identity
  explicitly through every spawn, route, message, snapshot, and settlement.
- Delegate execution to task/agent-core ports and persist through declared task
  or conversation operations. Do not call provider transports or SQL directly.
- Define one terminal outcome for each child and deterministic aggregation for
  the parent. Late messages, duplicate settlement, cancellation, and partial
  failure remain distinguishable.
- Bound depth, fan-out, concurrent children, messages, snapshots, context,
  retries, and total resource/token budget. Reject cycles and orphan work.
- Cancellation and shutdown propagate down the tree; cleanup cannot erase
  committed child evidence needed for parent recovery.
- Integration adapters project canonical events and cannot own hidden global
  coordinator state.

## Verification

Run focused `test_swarm_*` routing, snapshot, cancellation, budget, and
integration tests, plus task settlement/event tests when shared lifecycle
behavior changes.
