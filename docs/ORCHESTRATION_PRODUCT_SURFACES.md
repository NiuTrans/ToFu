# Orchestration product surfaces

This guide owns product-exposure policy referenced by the
[orchestration domain map](modules/orchestration_dag.md).

Orchestration remains experimental behind `debug_mode`. With it off, desktop
and mobile Workflows/Tasks navigation and saved-flow choices are hidden, stored
`activeFlow` is not restored, and future selection is cleared; an accepted turn
keeps its immutable snapshot. **Workflows** authors definitions, **Agent Mode**
selects the next turn's execution, and **Tasks** observes or mutates durable
runs.

Goal Mode is a durable-run specialization. The historical `autopilot` ingress
spelling selects the canonical worker ⇄ virtual-user FlowExecutor graph; no
environment flag or post-turn interpreter fallback exists. Its Studio template
does not create a duplicate Agent Mode choice.

Goal Mode is a frontend surface, not an async background swarm. Every settled
worker/virtual-user visible turn gets its own auto-translate trigger at the flow
turn-persistence boundary (`schedule_settled_visible_turn_translations`, called
from `OrchestrationChatTurnPersistence.__call__`), so intermediate replies
translate after they land instead of waiting for the run's terminal event. The
root turn still settles with the task's terminal event and remains the terminal
coordinator's responsibility. The coordinator skips per-turn-admitted child
turns so no turn is translated twice. Swarm sub-agents never cross this
boundary because they have no conversation attempt identity; their background
content stays untranslated and free.

Goal Mode leaf roles still execute in an isolated `SubAgent`, but that isolation
must not hide the visible parent turn's tool lifecycle. Flow explicitly installs
a canonical tool observer: every dispatch emits `tool_start`, `tool_result`, and
`tool_complete` on the current Flow turn as it happens. Each occurrence receives
a Flow-minted `toolCallId` independent of provider ids; the same id is persisted
on the bounded `tool_log` row and reused to build the settled turn's
`toolRounds`. The live task snapshot applies those frames as idempotent upserts,
so reconnect/poll sees the same in-flight rows and node completion replaces the
transient projection instead of appending a second, renumbered batch. Ordinary
swarm agents do not install this observer and remain isolated.

“Save & use” selects a saved definition only when its document token/revision
remain current, mode changes are allowed, and Studio closes. Conflict, failure,
intervening edits, or a busy chat leaves Studio open and `activeFlow` unchanged.
