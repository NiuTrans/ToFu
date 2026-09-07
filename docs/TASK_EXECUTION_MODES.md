# Task execution modes

This guide owns autonomous and collaboration-mode policy referenced by the
[task-engine domain map](modules/task_engine.md).

## Autonomous drivers

Flow-backed chat stores visible role messages as explicit related turns. An
accepted Goal turn starts one durable GoalRun; its worker ⇄ virtual-user Flow
owns all iterations and persists terminal meaning before chat completion.
Goal/Autopilot execution pins every leaf to the conversation's selected model;
its internal role tiers may shape the graph but cannot silently select another
model. Non-Goal Studio flows retain explicit role-tier routing.

Each leaf resolves that route once before dispatch and carries `modelRoute`
(`selectedModel`, `resolvedModel`, `role`, `tier`, `kind`) through live phases
and the durable visible Turn; a changed route is disclosed before the model
call and remains visible in the settled footer. The 40-iteration default shares
a 64 cap and requires `remaining=0`.

A VU stop verdict passes a backend acceptance gate before the loop ends. A
missing or positive `[PROGRESS:]` receipt is rejected outright, and the first
stop is challenged once when the producer changed real state while the verifier
used no tools of its own, or when close-out is entirely vacuous. The concluded
run's fold report comes from the sanitized, bounded terminal VU verdict, so
every run ends with its own explanation. Idle continuation creates a turn pair;
busy arming queues it behind the owner-scoped lane; a newer human turn
atomically retires or preempts it. VU events never tunnel into a settled parent.
Other dispatch is owner-bound.

Historical `tasks_pkg.autopilot` carriers serve only already-constructed tasks:
durable markers and public controls cannot activate them, and events stay
local. Close-out resolvers prefer owner-scoped settings metadata, inspect a
bounded 128-message tail, and load a full transcript only after an authoritative
unloaded-prefix miss. New behavior belongs in `lib/goal_runs/` or the shared
Flow chat boundary.

Every unattended loop needs a durable command id, owner-scoped lane decision,
finite budget, honest attribution, and terminal executor-start failure.

## Plan collaboration mode

`planMode: true` is one attended, read-only model/tool loop. Config resolution
and runtime normalization enable `ask_human` automatically and disable
autopilot, direct image generation, and selected orchestration flows. This
allows multiple clarification exchanges inside the same turn without asking
the user to discover a second toggle. The canonical schema stays within 325
tokens, bounds question/option payloads, and requires 1–16 options for choice
mode.

The model proposes rather than executes. Only the successful Plan-task terminal
boundary may mint a complete tagged plan into the typed turn sidecar owned by
`lib/plan_contract.py`; arbitrary assistant text is never upgraded into
execution authority. Leaving Plan mode manually does not synthesize an
execution prompt. Execution starts only through the v3 exact-plan command in
[`CONVERSATION_SYNC_V3.md`](CONVERSATION_SYNC_V3.md).
