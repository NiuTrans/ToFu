# Turn settlement contract

Settlement is a durable fact produced once by the turn authority. It answers
why an attempt ended and which recovery operations are truthful. The browser
projects this document; it does not recompute it from task events.

## Durable state

An output turn is live while its status is `pending` or `running`. Terminal
statuses are:

| Status | Meaning |
|---|---|
| `completed` | provider/agent finished normally |
| `interrupted` | cooperative user or supersession stop |
| `truncated` | provider limit or content filter cut output short |
| `failed` | generation, provider stream, or executor startup failed |

The terminal turn stores:

```json
{
  "outcome": "completed|interrupted|truncated|failed",
  "cause": "provider_finished|user_abort|generation_error|...",
  "evidence": "provider_finish|provider_stream_failure|...",
  "streamState": "provider_finished|premature_close|malformed_stream|...",
  "providerFinishReason": "stop",
  "error": null,
  "resumeOptions": []
}
```

`error`, when present, is a normalized error envelope. A terminal error event
without an error payload is itself a contract failure and is converted to an
actionable internal envelope at the authority boundary.

`streamState` is the closed parser verdict. `providerFinishReason` remains a
provider/compatibility detail and cannot prove success by itself on the typed
stream path. `evidence` records which positive or negative fact authorized the
turn verdict. A bare `done` event, non-empty content, or a parser default of
`stop` is not completion evidence and settles fail-closed. A named legacy seam
still accepts an explicitly supplied finish reason for non-stream producers;
new model-backed paths must carry `streamState`.

A provider stream that ends without its finish marker is never
`completed/provider_finished`. When partial prose exists, the live executor
first preserves that prose, emits a `phase: retrying` status, and performs a
bounded continuation from the preserved prefix. If those lossless
continuations are exhausted, the turn settles as
`failed/provider_stream_error` with `providerFinishReason: premature_close`;
the partial projection remains durable (and prefill-capable models expose it to
`continue`) and is never cleared by the whole-turn regeneration retry path.

## Single producer

`lib/turn_lifecycle.py::_settlement` derives the document from the terminal
executor evidence through the pure `lib/turn_verdict.py` state matrix.
`turn.event.record` commits projection, attempt event, turn
status, settlement, conversation revision, and any carried task event in one
Sidecar transaction.

Every terminal exporter uses the same matrix. In-process results and the
OpenAI/Anthropic compatibility surfaces call `derive_task_verdict`; a failed
verdict travels on the vendor error channel and is never normalized into
`stop` or `end_turn`.

Consequences:

- a client-visible terminal frame never precedes durable settlement;
- duplicate terminal frames are rejected by attempt state/CAS;
- a stale or superseded executor cannot overwrite the current attempt;
- reconnect and cold reload read the same settlement;
- queue draining happens only after the terminal transaction frees the lane.

## Resume options

Recovery is explicit. Each entry has an `operation` and a durable `anchor`.

- `continue`: lossless assistant prefill is available for a model that
  supports it and a non-empty partial projection.
- `checkpoint_resume`: completed tool rounds provide a stable checkpoint.
- `regenerate`: restart from turn input; always available for a non-successful
  terminal turn.
 The retry supersedes the whole lane tail: every turn
  after the regenerated turn, plus branch lanes rooted inside that tail, is
  discarded in the same transaction, so no client can observe a
  half-rewritten history. The command response reports the discarded ids in
  `deletedTurnIds` and the change log carries a `turn.deleted` entry, so the
  initiating client and peers converge without a snapshot.

The command service validates the selected operation against the stored
options and expected projection revision. The frontend displays exactly these
operations through `frontend/src/core/turn-presentation.ts`; it does not label
regeneration as continuation.

## Attempt startup failure

Creating a turn and starting its executor are separate durability phases:

1. create the input/output pair and pending attempt;
2. atomically claim the pending attempt;
3. register the executor task and bind its id before spawn returns;
4. if registration/start fails, settle the attempt with
   `task_start_failed`.

There is no post-spawn compatibility bind. Accepting an unbound worker would
recreate an orphan window in which two executors can own one attempt.

## Orchestration turns

Planner, worker, critic, virtual-user, scheduler, and swarm continuation rows
are explicit turns with stable identities and actors. They do not append
synthetic entries to an archived message array. A parent attempt may announce
related turn ids so its live stream exposes the complete orchestration shape.

## Invariants to test

- terminal state is monotonic;
- settlement and terminal projection commit together;
- stale attempts cannot mutate current turns;
- error envelopes survive replay;
- resume options correspond to actually executable anchors;
- startup failure leaves no pending/running orphan;
- terminal queue drain dispatches at most one successor.

Primary suites: `tests/test_turn_lifecycle.py`,
`tests/test_turn_event_carried_task_event.py`,
`tests/test_translation_turn_authority.py`, and
`tests/test_frontend_conversation_surface_vite.py`.
