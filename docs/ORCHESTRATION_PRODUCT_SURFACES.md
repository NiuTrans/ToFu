# Orchestration product surfaces

This guide owns product-exposure policy referenced by the
[orchestration domain map](modules/orchestration_dag.md).

Orchestration remains experimental behind `debug_mode`. With it off, desktop
and mobile Workflows navigation and saved-flow choices are hidden, stored
`activeFlow` is not restored, and future selection is cleared; an accepted turn
keeps its immutable snapshot. **Workflows** authors definitions and **Agent
Mode** selects the next turn's execution. Durable runs are observed through
the ordinary chat turn projection below and mutated through the run APIs
(abort, human-gate responses); there is no separate runs panel. The former
Tasks panel was retired because it re-projected the same durable run events
the chat already renders — a second, diverging read model with no added
intervention power.

Goal Mode is a durable-run specialization. The historical `autopilot` ingress
spelling selects the canonical worker ⇄ virtual-user FlowExecutor graph; no
environment flag or post-turn interpreter fallback exists. Its Studio template
does not create a duplicate Agent Mode choice.

Each sequential Goal phase after the first allocates its running Turn before
dispatch. Deltas, reasoning and tools use that Turn's ordinary attempt patches,
coalescing and keyed rendering; settlement updates the same identity. The root
attempt remains the executor authority and retains the first phase's committed
text, tools and segments. Child writes prove the live root in the same storage
transaction. Stopping from any phase targets the executor; root settlement also
settles its active child without discarding partial output. Reconnect reads the
same Turn snapshots and ordered events as normal chat. Virtual-user close-out
retains its explanation/reasoning/tools while stripping machine verdict tokens.

Completed-phase persistence sends only the newly completed suffix, with stable
offset identities and idempotent retry. It does not retransmit prior phase
documents. The current live projection baseline is task-local and released at
phase settlement or terminal task cleanup. `tests/goals/test_goal_context_window.py`
measures fixed-size input and linear transmission over the 64-iteration cap;
the storage contract tests cover streaming, reload and cancellation parity.

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

Terminal auto-translate runs on the durable `_translate_runtime` TaskRuntime,
not the in-memory accumulator. Every settled turn — the regular-chat root turn
(from `_translate_settled_turns`), goal-mode child turns (from the per-turn
persistence boundary), and boot-sweep re-spawns — commits its whole-turn
deliverable through a durable runtime translation task
(`_spawn_whole_turn_translation`). If that task dies on a transient dispatch
failure (provider stream premature close, engine deadline exhausted), the
worker's settle-error path persists a `_translateFailed` verdict on the turn
projection (`mark_turn_translation_failed`) instead of the turn vanishing
silently; the marker is cleared by the next successful commit and never
regresses a committed turn. A boot sweep (`run_boot_translation_sweep`, wired
into the post-ready path) re-spawns whole-turn translations for recently-active
conversations whose settled turns still lack a translation — covering both a
recorded failure and a restart that dropped the in-flight task. The
`_Accumulator` survives only to translate live per-round previews during
generation and to stamp those cached translations onto the projection's
segments at settle (`finalize_incremental` is now stamp-only); it no longer
owns the deliverable. Swarm sub-agents remain outside this backstop because
they never carry conversation attempt identity.

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
`segments` list that collapsed the settled surface into content plus a trailing
tool panel. Historical flow rounds may contain only bounded previews
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
