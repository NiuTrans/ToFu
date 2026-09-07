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

A settled Goal Mode turn is durable-rendered exactly like a normal chat
turn. The `turn.visible.sync` write boundary assembles the segment timeline
(narrative ⇄ tool_use interleaved, terminal text last) with the same
assembler normal turns checkpoint through, instead of persisting an empty
`segments` list that collapsed the settled surface into content plus a
 The flow projection's rounds are bounded previews
(`query` brief + result snippet, no `toolArgs`), so the boundary fill-only
inherits the missing display fields (`toolArgs`, `toolContent`, timing,
execution identity) from the root turn's live-checkpointed projection,
matched by the stable `toolCallId`, before segment assembly — the durable
rows render full command cards without growing the sync payload, and the
flow projection's own values always win. Once a run owns visible turns, the
lifecycle fold likewise preserves the first turn's committed `content` and
`thinking` instead of refolding the per-node task buffer that
`flow_iteration` resets. The swarm substrate stamps `edited_path` /
`edited_action` on each edit-tool `tool_log` row at dispatch time; the flow
chat adapter projects those markers into the turn's
`modifiedFileList`/`modifiedFiles`, so the settled file-changes block renders
from the same projection fields a normal turn's journal derive produces.
`run_command` rounds additionally harvest their flat result identity
(command line, exit code, terminal badges) into the row's `result_meta`,
and the bounded historical-row compaction now reclaims only prose bodies
and heavy list values — `args_brief` (≤200 chars, the card's `$` line) and
the flat meta scalars survive — so settled command cards keep their command
line and exit pill instead of degrading to a bare `$`.

“Save & use” selects a saved definition only when its document token/revision
remain current, mode changes are allowed, and Studio closes. Conflict, failure,
intervening edits, or a busy chat leaves Studio open and `activeFlow` unchanged.
