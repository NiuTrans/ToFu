# Tool invocation dispatch

Public callers use `api.py`; `_pipeline.execute_tool_pipeline` owns admission,
canonical result settlement and ordered replay. See `docs/modules/tools_execution.md`.

- `_execution` starts every admitted call at once (there is no serial lane),
  collects completions and cleans up workers. Its batch borrows
  invocation-local dictionaries; it never publishes another task state machine.
  Ports preserve execution, collection, heartbeat and settlement seams.
- `_ordering` names the only call-order exceptions: same-target file, desktop
  file, memory and browser-tab mutations, `todo_write`, and `run_command`
  directories frozen at admission. Ordered children of a sibling gateway call
  also wait for the earlier-issued gateway calls of its response
  (`GatewaySiblingOrder`). `api.plan_concurrent_round` exposes the same
  rules to the native gateway, whose scheduler keeps them in Rust.
  Approval and hooks run on the call's own worker; settlement is serialized by
  the invocation's settle lock.
- `_reuse` checks cache generation, file freshness and tool-search projection
  before delivering a cached result through the same settlement seam.
- `_flags`, `_approval`, `_prefetch` and `_heartbeat` retain partition, approval,
  occurrence-bound prefetch and declared-deadline authority.
- Result publication may happen as each tool finishes; model replay remains in
  provider call order. A collector gap must harvest real receipts without
  replaying effects. Timeout/Stop must not let late workers overwrite outcomes.

Verify collection, parallel deadlines, outcome finality, write approval/freshness,
call identity, prefetch, `tests/tools/test_parallel_dispatch_overlap.py` and
`tests/tools/test_tool_settle_all_lanes.py`, `tests/tasks_pkg/test_gateway_sibling_concurrency.py`;
then native gateway, event ordering
and hardware-unlimited policy neighbors.
