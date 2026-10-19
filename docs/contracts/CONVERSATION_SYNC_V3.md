# Conversation Sync v3

Responsibility: the sole command, snapshot, replay, recovery, and health
protocol for a conversation. The canonical wire source is
`contracts/conversation_sync_v3.yaml`; generated Python and TypeScript files
must never be hand-edited.


String `minLength` and `maxLength` constraints count Unicode code points, not
UTF-16 code units or UTF-8 bytes. Generated browser validation must agree with
Python for supplementary characters (including emoji), including nested
background-command previews in snapshots and history pages. A valid 2,000-code-point
preview must not prevent opening its conversation. Canonical projection normalization
bounds historical background-command `command` and `resultPreview` display text
using the schema limits, including `_bgCommandInjects[*].previews[*]`, and marks
clipped output `resultTruncated`. It copies metadata without changing stored history
or full model-facing text. Identity, status, malformed types, and other invalid
wire values still fail validation; do not bypass snapshot validation to recover
from a presentation-field error.

Tool-result evidence preserves its recorded `resultContractVersion`. Readers
accept the current `tofu.tool-result/v2` and historical `tofu.tool-result/l0`
identities; writers continue to emit v2. Historical l0 evidence does not need
a storage rewrite to appear in snapshots or replay. Unknown versions, extra
fields, and invalid evidence values remain contract violations.

## Committed file changes

Committed file projections retain `changeId` and `rootPath` from the project
journal through live updates and settlement. Repeated edits of one file change
its receipt while preserving the unique-file count. These fields support
DOM-independent consumers such as [paper preview](../PAPER_BUILDS.md#chat-preview);
they never grant project access. Failed journal writes emit no commit receipt.

Auxiliary manual worktree entries retain their actual checkout `rootPath` and
optional per-file `undoAvailable: false`, including after Git merge/removal.
An explicit per-file denial disables undo/redo for the whole block without
suppressing its committed file rows. Both projection and command admission use
the same capability policy; this is not permission to undo the canonical merge.

Device writers use [`project_file_receipts_v1.schema.json`](../../contracts/project_file_receipts_v1.schema.json).
The receiving task validates the complete receipt and requested paths, then
binds its authenticated owner and addressed `remote:<device>:<root>`. The
original tool output remains separate. Settlement preserves remote facts
without reading a server-side journal for the remote path.

Turn file presentation retains at most 512 unique rows; `modifiedFilesTruncated`
and `fileChanges.truncated` expose omitted rows and survive settlement/resume.
Durable journal and device snapshots remain intact. `undoAvailable: false`
disables the local undo control for external or incomplete file sets; the
command boundary independently refuses them before any projection or file
mutation. Verify with receipt, round-derivation, file-command, resume and
conversation-surface tests, plus the remote paper browser journey.

## Goal phase execution

`turn.visible.sync` accepts a bounded `start_index` plus message suffix; `live`
admits one Goal phase before dispatch. Stable phase identities reuse the first
command Turn and mint child attempts thereafter, with a hard 4,096-phase
ceiling beneath the executor's tighter budget. Completed rows are replay-safe;
an existing live child settles through `turn.event.record`. The child's durable
`flowRootAttemptId` config must match the write's `flow_root_attempt_id` lock
hint and a current, owner-matching root. Parent-before-child attempt locking,
root terminal propagation and sync publication share the transaction.
`turn.attempt.get(resolve_executor=true)` resolves the same relationship for
stop commands, including registry-miss recovery. No browser schema or alternate
stream is introduced.

Problem: after a Flow child writes a file, the producer derives `fileChanges`
from `modifiedFileList` and later patches its `taskId`. Native storage previously
patched the raw document without that sidecar and rejected the valid nested path.
Decision: `turn_projection_segments::normalize_file_changes` shares file-sidecar
normalization between the locked patch base and settled native assembly. Replay
still starts from the exact committed document and includes the sidecar creation.
Alternatives rejected: weakening missing-parent validation hides corrupt patches;
adding a field on public reads without a revision breaks replay. Verify the native
projection/replay tests and real native Flow file-tool HTTP cases.
Completion-only Flow phases also retain accepted system notes while assembling
tool segments. Dropping those notes on first settlement made an identical final
sync appear to add evidence and fail its immutable settlement check. Native
assembly reuses the common note validator and preserves round/attempt placement;
exact replay remains a comparison against the sealed result.

Projection normalization must remain visible in the revision patch: its base
is the exact previous wire document, even when the producer's local baseline
has already normalized missing segment placeholders. The first nonempty content/reasoning
channel forces one structural write before later deltas use text-only patches.
Otherwise a missing block can cause a render gap or a full-snapshot loop.

An admitted Goal phase seals its own live `segments` and `toolRounds` through
`lib/agent_core/output_settlement.py`, rather than rebuilding streamed history
from a bounded completion preview. Recorded reasoning, narration, full results,
and block identities survive resets and terminal settlement; completion may
append genuinely new output or extend a cumulative prefix. Occurrences match
by producing execution, exact Inspector token and round/call identity, never
by result text. Legacy completion-only imports retain a fill-only compatibility
path; they do not define new execution semantics.

Tool identity belongs to the producing invocation. A parent or visible phase
may carry child output but cannot replace its `taskId` or add the display
attempt's `attemptId`. Root/resume backfill only fills unknown fields within
the same execution. This keeps one tool occurrence through checkpoint, seal,
and cold reload even when root/child counters coincide. New cards pass the
persisted `inspectorRef` unchanged to the owner-authorized request/state API;
invalid explicit references never widen into a legacy owner guess.

The final live checkpoint must commit before phase settlement. A lost ACK may
retry only the identical checkpointed output and ownership fence; Sidecar still
checks attempt state and revision. Failure prevents successor dispatch or a
successful terminal frame. Unknown usage is omitted, never serialized as
`usage: null`, so the same strict v3 projection decodes live and after reload.

## HTTP major and native response contract

Conversation schema `tofu.conversation-sync/v3` is served on `/api/v4`.
The schema number does not choose the HTTP major. The generator validates
paths against `contracts/api_v4.yaml`; route parity tests cover every method.
Old `/api/v3` clients receive the release gate's 426 upgrade problem.
The sync blueprint explicitly declares `NativeContractBlueprint` with its
generated contract ID: snapshots, command receipts and domain errors retain
the sync protocol's shape. Generic v4 wrapping must not discard `latestTurn`,
validation violations or storage retry fields. Declaration follows the matched
blueprint, never a URL-prefix exemption, and changes no auth/owner checks.
The generated image/event/history/command URLs migrate together.

## Snapshot live-phase parity

Both SQL and native TofuDB snapshots implement the existing `activeAttemptPhase`
contract. Native `turn/snapshot_phase.rs` reads the current running main-lane
attempt's event head and at most one 32 KiB logical event under the same snapshot
transaction. It validates exact conversation/Turn/Attempt/sequence identity and
requires the event revision not to exceed the returned Turn revision. The byte
witness is checked before overflow-blob materialization. Null, missing, oversized
or phase-free latest events omit the field; terminal/superseded attempts and
foreign owners cannot contribute it. No backward phase search or timing-history
inference is permitted.

Problem: native snapshots omitted this already-declared field, so cold loads and
resync cleared the browser phase and fell back to a waiting label despite live
execution. Decision: restore backend parity at the read authority, preserving
intentional phase clearing. Alternatives rejected: retaining the old browser
phase can display a superseded attempt; synthesizing from diagnostic history
resurrects stale phases. Specs: native `snapshot_phase_tests.rs` and
`test_snapshot_restores_only_current_committed_bounded_attempt_phase`.

## One authority

```text
generated command DTO
  → routes/conversation_sync_v3.py
  → ConversationTurnCommandService
  → turn lifecycle + semantic storage operation
  → turn/attempt/change rows in one transaction
  → commit ACK
  → best-effort wake
  → one conversation SSE coordinator
  → one turn reducer
```

A snapshot and ordered conversation events are the only inputs that mutate
browser turn state. WebSocket push and BroadcastChannel frames only invalidate
the coordinator. They never carry an authoritative projection. The browser
accepts a push frame only when the pure `frontend/src/core/frame-identity.ts`
policy matches its explicit owner ID to the authenticated local owner; missing,
unresolved, or foreign identities fail closed.

## Immutable streaming state

Problem: every event copied whole entity indexes and snapshots repeatedly
sorted history. Decision: `core/turn-state.ts` owns one synchronous draft;
`core/entity-record.ts` splits oversized hash leaves and copies touched paths.
Fanout is an algorithmic layout, not a resource cap. Published records revoke writes;
old snapshots share unchanged DTOs. No event chain or second authority exists.
Reads/reflection/freeze/JSON remain supported; use `entityRecordValues/Entries/Keys` for bulk reads.
Materialize a plain record before structured cloning; proxies are never wire payloads.
Snapshot insertion preserves stable ordinal ties without sorting every prefix.
Duplicates/stale events retain identity; recovery callbacks remain intact.
The reducer publishes changed IDs/reset through the existing subscription.
`createConversationViewModelSelector` retains one compiled view per mounted
session and unions IDs until paint; local changes replace affected ancestors.
Presentation maps are snapshotted. Structural/cross-Turn changes, missing
deltas and overlays rebuild the same composer. Dispose releases joins and IDs.
Memory scales with loaded entities/lanes, not the number of streamed events.
Rejected: full copies (linear writes), mutable state (lost isolation), and
prototype/event chains (history retention or compaction stalls).
Specs: `entity-record.test.ts`, `turn-state-sharing.test.ts`, `view-model/selection-deltas.test.ts`;
browser: `tests/visual/test_visual_streaming_state.py`.
Paired offline diagnostics: `benchmarks/turn_state_measure.mjs`,
`benchmarks/turn_selection_measure.mjs`, `benchmarks/turn_index_measure.mjs`.
Client DOM measurements and shell receipt caching: [rendering performance](../TURN_RENDERING_PERFORMANCE.md); offline reducer clocks alone never admit DOM/startup/I/O latency.

## Owners

| Concern | Owner |
|---|---|
| Paths, DTOs, events, retries, health policy | `contracts/conversation_sync_v3.yaml` |
| Contract generation | `scripts/gen_conversation_sync_contract.py` |
| HTTP/auth adapter | `routes/conversation_sync_v3.py` |
| Command policy | `lib/conversation_sync/command_service.py` |
| Proposed-plan and execution-handoff documents | `lib/plan_contract.py` |
| Snapshot/replay service | `lib/conversation_sync/service.py` |
| Repository protocol | `lib/conversation_sync/repository.py` |
| Atomic durable operations | `lib/storage_authority_sql/operations_pkg/_turns.py` (facade over `_turns_{core,read,write,lifecycle,events,branch}.py`) |
| Post-commit wake | `lib/conversation_sync/broker.py` |
| Browser cursor/SSE/recovery | `frontend/src/core/conversation-sync.ts` |
| Browser authenticated owner lifecycle | `frontend/src/core/current-user.ts` |
| Browser push-frame ownership policy | `frontend/src/core/frame-identity.ts` |
| Browser turn state | `frontend/src/conversation/domain/turn-store.ts` |
| Browser runtime/projection | `frontend/src/core/turn-runtime.ts` |

## Public surface

| Operation | Endpoint |
|---|---|
| Snapshot/reset image | `GET /api/v4/conversations/{conversationId}/sync` |
| Older lane page at an exact replay head | `GET /api/v4/conversations/{conversationId}/turns/history` |
| Complete Turn for explicit projection editing | `GET /api/v4/conversations/{conversationId}/turns/{turnId}` |
| Exact lazy historical tool output | `GET /api/v4/conversations/{conversationId}/turns/{turnId}/tool-results/{roundIndex}` |
| Ordered replay SSE | `GET /api/v4/conversations/{conversationId}/events` |
| Create input/output pair and attempt | `POST /api/v4/conversations/{conversationId}/turns` |
| Projection CAS update | `PATCH /api/v4/conversations/{conversationId}/turns/{turnId}` |
| Execute an exact proposed plan | `POST /api/v4/conversations/{conversationId}/turns/{turnId}/plan/execute` |
| Regenerate/continue/resume attempt | `POST /api/v4/conversations/{conversationId}/turns/{turnId}/attempts` |
| Create/delete lane | v3 lane operations generated from the contract |
| Delete explicit turns | `POST /api/v4/conversations/{conversationId}/turns/delete` |
| Abort attempt | `POST /api/v4/attempts/{attemptId}/abort` |

Every operation receives authenticated `user_id` explicitly. No v2
conversation route or attempt-scoped conversation EventSource exists.
`regenerate`, `continue`, `checkpoint_resume`, and `answer_guidance` supersede
the whole later lane tail, including branch lanes rooted there, in the same
transaction. Resume keeps the target checkpoint; regenerate starts over.
A live descendant refuses the command before any mutation or task dispatch.
The response carries `deletedTurnIds`; `turn.deleted` replay makes peers
converge. Clients remove the suffix only on authoritative acknowledgement,
never on click, and an older response cannot resurrect it.


### Unconfirmed-usage forward recovery

**Problem:** a closed unknown-usage execution had no nontechnical recovery path.
**Decision:** `GET .../turns/{turnId}/usage-recovery` inspects one owned settled
source, its saved configuration and tool receipts without a model call or write.
Supported uncertain file publications use their existing domain inspector;
unknown/background/delegated effects without complete proof refuse. This check
cannot reconstruct missing provider usage. `POST` requires `confirmed: true`,
the exact preview budget and source projection revision. It creates an ordinary
human/assistant pair with finite token/tool/time limits and a `usageRecovery`
confirmation receipt. Source revision, idle lane, lane tail and pending-human
fences commit in the existing `turn.create_pair` transaction on both stores.
The old Turn/Attempt and unknown accounting are unchanged. Only user-facing
preferences cross into the new config, including write-approval/readonly restrictions
and the selected browser device; old checkpoint, wallet, Goal and Flow
authority does not. Command identity owns lost-ACK retries, never another
execution authorization. The browser aborts disposed checks, fences late replies
and refreshes the existing snapshot owner after acceptance.
**Alternatives rejected:** resuming the unknown checkpoint bypasses its financial
fence; ordinary createTurn lacks source-revision/confirmation policy; a second
recovery queue or ledger duplicates existing ownership. Specs:
`tests/conversation/test_usage_recovery.py`, renderer `usage-recovery-notice.test.ts`
and `tests/visual/test_visual_usage_recovery_actions.py`.

### Live human-guidance acceptance

**Problem:** `ask_human` formerly resolved only an in-process waiter; losing that
waiter or a command ACK could lose an answer on restart. **Decision:** the existing
owned `POST .../turns/{turnId}/attempts` command accepts
`operation: answer_guidance` with an explicit `guidanceId`, `humanResponse`,
`commandId`, and `expectedProjectionRevision`. This scoped variant does **not**
create/supersede an attempt, delete a lane tail, or dispatch work. It validates the
exact persisted ask_human round and its declared choice labels (including the
legacy optional `\nuser_note: ` suffix), then commits the answer on that round
through `turn.projection.update` before waking the live gate. Streaming revisions
may advance independently: question identity and stored options, not an unrelated
text revision, fence live acceptance. Absent/foreign questions return not-found;
invalid choices are rejected; conflicting second answers return conflict.

The public reply is the existing `TurnCommandResponse` with the **original**
current attempt. Acceptance proof is the matching round's `guidanceResponse`,
`guidanceAnswerCommandId`, completed status and replayable `toolContent`, not the
original attempt's commandId/operation and never a waiter ACK. Equal-answer retries
return `idempotentReplay` without a new revision or dispatch, even after settlement.
Producer events preserve these receipts. The running executor polls the owned
Turn receipt if its waiter disappears; restart recovery retains a completed tool
result for checkpoint replay. The unscoped stopped/interrupted answer command keeps
its existing resume semantics. Legacy `/human_response` remains compatible: a
locally registered conversation-scoped gate also commits through this operation;
legacy non-conversation gates retain their existing volatile lifecycle.

**Alternative rejected:** a separate answer store or model-selected answer URL;
these would split ownership/replay authority. The browser uses only the generated
`conversationSyncApi.turn` and `.createAttempt` helpers with trusted conversation
and turn identities. It submits `guidanceId` only for live pending/running questions
and validates the returned round receipt. Storage growth is one bounded receipt
on an existing round (8,000 response characters, 256 command-id characters), no
new worker/store. One owner-bound read per existing two-second gate wait interval
is the fallback consumer; process-local notification still accelerates delivery.

A user Turn's `contextSnapshot` is historical evidence: changing project
folders or composer controls alone never rewrites it. The project panel stages
folder edits until **Apply Changes** succeeds. A later explicit `regenerate`
then sends the input Turn's current projection plus a newly captured live
context as `inputUpdate`, guarded by `expectedInputProjectionRevision`; storage
applies that input update and creates the new attempt in the same transaction.
**Edit + resend** uses this same atomic command rather than PATCHing the input
first. Consequently an accepted regeneration shows and executes with the
updated workspace, while untouched earlier Turns continue to report the
context they actually used. Resume operations (`continue`,
`checkpoint_resume`, `answer_guidance`) may send a `contextSnapshot`-only
`inputUpdate` so the submitted Turn's info rail records the live model/tools
at resume without rewriting submitted content. Whenever `inputUpdate` is
present, `expectedInputProjectionRevision` is a non-negative integer; an
`undefined` or omitted-as-undefined property is not an integer and the
generated browser validator rejects the command before it reaches storage.

Regenerate also rebinds the reused generated Turn's durable `actor`/`kind` to
the normalized target interaction mode in that same transaction. Plan produces
`planner`/`plan`; Flow produces `assistant`/`flow_node`; Standard and Goal
produce `assistant`/`reply`. The command response and first attempt event carry
the migrated identity, so the initiating Store, peer replay, and later snapshot
all converge without presentation-layer role overrides.

## Background-command completion evidence

`BackgroundCommandCompletion` is bounded presentation of an existing durable
result, not a second execution state machine. `ConversationQueueItem`,
`TurnProjection`, and injection previews carry the same command ID, finished
status, command, result preview, and truncation flag. Finished does not imply
exit success. Queue membership means delivery is not yet confirmed. Model-facing
content remains unchanged; the browser renders typed evidence instead of its
notification wrapper. Both queue-list and snapshot readers use the shared pure
`lib/background_command_projection.py` projection. Specs include
`test_background_completion_queue_projection.py` and `test_background_completion_ui.py`.
New completion rows have passive `background_receipt` kind and never start a
Turn individually. Earlier `workflow_step` completion rows remain readable and
are skipped by ordinary dispatch after an upgrade. Once all known commands for
the owner and conversation have published, a single workflow wake starts an
idle Turn; queued human or other work takes priority and consumes the receipts
without an extra wake. That Turn rehydrates up to 32 durable receipts into its
background-command inbox lane; the coalesced inbox message presents them at
one model boundary, and the confirmed receipt ACK removes only those rows.
Remaining receipts stay visible for a later Turn. Startup and maintenance scan
passive receipt summaries separately to rebuild missed collective wakes.
The queue's `preview` list mode and conversation snapshot bound each item's
display text to 2,000 characters; the `background_pending` mode alone reads
the complete model-facing payload, capped at 32 rows per real turn.

For mid-turn delivery, `TurnInjectionPreview.queueId` links the receipt to the
exact durable delivery intent (distinct from execution `commandId`).
`turn.event.record` commits receipt, eligible queue deletion/renumbering, task
replay carrier and `AttemptEvent.payload.removedQueueIds` in one transaction.
The reducer applies removals only after the receipt's attempt/sequence/revision/
patch validation: subscribers never see an accepted receipt beside its old
pending card. Remaining queue ranks are derived in the same priority/position/ID
order as storage renumbering, so live cards agree with cold snapshots. No DOM
matching, polling or browser delivery ledger is involved.

The writer checks owner/conversation, command identity, passive or legacy
workflow kind and absent
Turn/Attempt links. A present protected/mismatched row rejects the whole commit.
Event/carrier/delete failure rolls back both halves and cannot leak a confirmed
executor sidecar through later checkpoints. The emitter retains one frozen
receipt and makes at most two commit attempts per verified provider boundary,
without another model call; stable block identity makes lost-ACK rebase
idempotent. Pending delivery is capped at the existing 256-item inbox bound;
excess twins stay in the bounded inbox, backed by durable queue rows. Successful
commit clears only that batch and fences late twins. Missing rows are safe on
replay; old receipts without queue identity never authorize inferred deletion.
Provider consumption itself is not transactional: crash before the authority
commit may still redeliver. Specs: `test_background_delivery_receipt.py`,
`test_background_delivery_ui.py` and `test_queue_injection_ack.py`.

A pending background completion does not block immediate user insertion: a
`steer` submission commits its own injection on the live Turn and leaves the
completion's durable queue row and inbox twin pending. The API boundary is
covered by `test_conversation_command_inject_mode.py`.

When hydrating a lane-busy conflict fails with a storage error, preserve the
typed storage failure and its retry contract at the HTTP boundary (temporary
capacity failures return 503). A successfully read but absent live Turn uses
the existing durable queue fallback, with `steerFallback: true`. Problem:
masking a failed read as an empty conflict caused a null dereference and a
redacted 500 toast. Decision: preserve failed-read authority; tolerate an absent
Turn only through durable queue admission. Alternatives: silently treating
resource exhaustion as a missing attempt hides the cause; using cached task
identity risks delivering to a stale attempt.


The v3 queue-cancel command also accepts pending background completion rows
without a Turn pair. It removes only delivery intent, returns `inputTurn: null`
and no deleted Turns, and atomically publishes `removedQueueIds` for replay.
Already-leased delivery conflicts rather than claiming to stop dispatched work.
After commit, the application removes/fences the process-local inbox twin;
completed command execution evidence and unrelated queued inputs are preserved.
Specs: `test_background_completion_cancel.py`.

Completion publication retries transient storage errors at most three times
with a frozen payload and owner/conversation/command-derived queue identity.
A lost ACK replays the same durable receipt. Retry sleeps total at most two
seconds, without another worker or retry queue. Explicit command queries expose
`delivery_status`: pending, queued, failed, or not_applicable. Before the first
durable commit, process failure can lose the result; exhausted steer-handoff
publication is logged, not restart-recoverable. Deferred acknowledgement requires
positive verified provider completion, never an error-dictionary return.

The process-local inbox fences the last 16,384 `(slot, queue ID)` pairs in FIFO
order without payloads. Dispatch fences even an absent twin; enqueue rejects
late publication under the same lock. Clear/revival preserve fences. Protection
ends at FIFO eviction or process shutdown, and retains the existing slot
ownership boundary. This is not durable delivery history.

## Snapshot boundary

The snapshot reads one owner-scoped transaction and contains:

- conversation revision and ordered replay sequence;
- opaque cursor, server boot identity, and heartbeat interval;
- public conversation settings;
- `pushWithheld` — the read-side delivery-wedge signal (see Health);
- when explicitly selected, `hasArtifacts` — one metadata-free existence bit;
- the authoritative Turn window and attempts required to render and reconnect.

Internal migration markers are not public settings. The browser applies
settings and turn state from this one response; it never follows with an
archive/settings fallback request.

The cursor is the exact read boundary: state at or before it is present in the
snapshot; later committed state is replayable after it.

Pending/running projections preserve the exact committed revision document in
snapshots, history pages, command responses, and embedded replay Turns. Public
read adapters must not append terminal placeholders, repair segment chronology,
or refresh stale text/thinking mirrors at the same revision: a subsequent
positional `append`/`append_text` would otherwise apply to a different base,
duplicate live narration, or shift tool paths. Slim writes intentionally update
only the cumulative channels; the renderer reads those channels for terminal
blocks without rewriting Store state. Producer-side normalization remains
explicit, and Sidecar includes its repair delta in the next committed structural
patch. Settled legacy reads keep their existing compatibility normalization.
Specs: `test_live_snapshot_and_command_bases_replay_without_duplicate_narration`
checks every replay revision from early snapshots, with stale text/thinking mirrors
and cold producer caches. `tests/e2e/test_e2e_pending_narration.py` holds the early
command and tool-execution windows open in Chromium, then checks cold reload.
It is included in `make test-e2e`, not just the wider visual tier.

Persisted turns decode fail-closed: a stored shape the schema no longer
accepts raises `ContractViolation` instead of silently mis-rendering.
Declared legacy shapes cross the boundary only through
`TURN_READ_ADAPTERS` in `lib/conversation_sync/service.py` — the explicit,
ordered registry of copy-on-write lifts (today: pre-`attemptId` client
observations). Every registered adapter is pinned by a legacy fixture
under `contracts/fixtures/sync_v3/` that must fail raw decode and pass
after adaptation (`tests/conversation_sync/test_wire_fixtures.py`); a shape without a
fixture is not an adapter.

The endpoint defaults to `segmentPayload=full` and a complete Turn set for
independent and older clients. The generated browser client fixes
`segmentPayload=refs&toolPayload=lazy&turnWindow=tail-96&artifactHint=has-any`. The artifact
selector is an additive wire-shape opt-in: the owner-scoped snapshot transaction
uses a bounded `LIMIT 1` existence query on SQLite and returns `hasArtifacts`,
never artifact metadata. Native TofuDB uses its existing conversation artifact
count to certify only an empty result; positive counts include tombstones, so
it omits the hint and leaves live presence to the owned artifact-list reader.
This is one owner/tenant-scoped point read, without new indexes or blob reads.
Returning a constant `false` hid existing deliverables; treating a positive
count as proof of live presence would misclassify deleted ones.
`false` lets the browser commit an empty artifact read model
without the legacy artifact-list HTTP request. `true`, or a missing field from
an older server/sidecar, retains that list request. A request without the
selector receives the old response shape, and the selector participates in the
admission and snapshot-flight keys so old and new clients cannot share a
differently shaped response.

A linear, main-lane-only conversation
returns at most its newest 96 Turns, only their current attempts, and a
`turnWindow` carrying the exact older-page ordinal, `hasMore`, and durable total.
The bounded SQL reads `LIMIT + 1` through the owner/lane/ordinal index; it never
loads omitted projections merely to slice them in Python. A conversation with
any branch lane deliberately falls back to the complete snapshot until a
bounded lane directory can preserve every branch's discoverability. Thus the
optimization cannot make durable branch state invisible.


**Viewport and byte-aware initial history.** Problem: 96 Turns is not a byte
bound; a live browser sample still received 32,437,621 decoded JSON bytes with
refs/lazy enabled. Decision: the browser supplies additive `windowLimit` and
`windowBytes` selectors. The retained host probes the actual chat pane; the pure
policy requests `ceil(height / 160) * 2 + 4` Turns (8..96) and a soft projection
budget scaled from 1 MiB at 960×800 (256 KiB..16 MiB). Older-history demand uses
the same byte budget with its existing count and exact replay-head fence.
Missing/invalid viewport dimensions use 960×800. No prefetch pool, worker,
resident snapshot cache or durable-state truncation is introduced.

The semantic `turn.sync.snapshot` / `turn.sync.page` operations accept optional
`max_projection_bytes`: 0/omission preserves legacy count-only behavior;
positive values admit 64 KiB..16 MiB. SQL selects owner/lane-filtered UTF-8
projection/checkpoint/patch-size witnesses before reading selected payloads.
Native reads use stored-document/checkpoint/text-tail size witnesses before
materialization. Both retain a newest contiguous suffix, always admit one
indivisible Turn, and keep cursor, total, current attempts and replay head in
one transaction. This is a soft projection estimate, not a strict HTTP-body
ceiling: metadata, JSON escaping, patches and an oversized single Turn can
exceed it. Full/branched snapshots retain complete topology. Different byte
budgets participate in snapshot single-flight and browser admission identity.
Alternatives rejected: slicing already-decoded history does not save storage
work; a hard bound that drops an oversized Turn strands its history cursor;
fixed small windows leave large reading panes underfilled.

Successful snapshot responses expose content-free `Server-Timing` metrics:
`snapshot` includes scheduling, storage and projection/validation; `encode`
measures response encoding. Proxy waiting, transfer and browser rendering remain
separate measurements.
Problem: a fixed tail/history page can exceed native response capacity and
repeatedly fail with `database_resource_exhausted`. Decision: after that exact
storage failure, bounded repository reads halve their requested Turn count down
to one. Every successful response remains one owner-scoped storage transaction,
with its original replay head, current attempts, and continuation cursor. Normal
reads retain their requested count; unwindowed snapshots and failures at one
Turn remain errors. Alternatives: retrying an identical page cannot resolve a
byte bound; slicing a decoded page cannot recover a rejected response and would
break cursor completeness. No durable records are removed or rewritten.

Older history uses the generated `turnPage` operation. The caller supplies its
currently applied `syncSeq`, lane, exclusive `beforeOrdinal`, and a page limit
of at most 256. Storage checks owner and replay head in the same read
transaction, reads `LIMIT + 1` oldest-first, and returns only current attempts
for those Turns. A stale head returns 409 and never publishes a page. In the
browser, identical requests share one flight, a different overlapping request
is rejected instead of queued, and identity/reference/sequence validation plus
the non-authoritative Store merge happen in one synchronous coordinator turn.
An SSE or reset that advances local sequence before publication rejects the old
page. The Surface's 80-Turn DOM window prefetches a 64-Turn page only after its
local prefix is exhausted, anchors on stable `turnId`, and reveals 20 newly
prepended Turns without replacing the render path; the remaining 44 serve the
next local window moves without another API round trip.

In the reference representation, on terminal
Turns, a uniquely matched settled `tool_use` segment keeps its ordering identity
and `roundRef` while omitting duplicate input/result bodies already owned by
the sibling `toolRounds` entry. Non-completed Turns require the sibling round
itself to be `done`; ambiguous or unfinished blocks stay full. The same
browser-only view removes opaque
`_responsesItems` / `_anthropicContentBlocks` provider replay bodies from those
completed rounds; they reconstruct future server-side model requests and have
no browser consumer. Completed `apiRounds` keep the browser's closed-world
token facts, exact CNY round total, cache-break, dispatch identity,
subscription quota, and trace facts, but omit unused USD/itemized cost fields,
stream counters, route/cache diagnostics, and pricing snapshots that are
server evidence only. Running, interrupted, truncated,
failed, or ambiguous segments and every ordered replay event remain full. The
coordinator resolves each reference and attaches the same round/input/result
objects before publishing the snapshot to TurnStore; an invalid reference is a
protocol error, never an empty tool card. The reference view is request-local
and does not mutate the shared authoritative snapshot or create another stored
format.

For terminal Turns, `snapshotProjectionRefs` removes exact stable-segment
duplicates with a global 4,096-reference ceiling. `projection.content` must be
at least 128 encoded bytes; both it and a tool round's `thinking` must save at
least 64 bytes after reference overhead and match one uniquely identified text
or thinking block. Duplicate Turn/call/block IDs, ambiguous text, active Turns,
or non-beneficial values remain inline. The browser rejects
missing/active/ambiguous sources and inline conflicts, restores `content` and
round `thinking` from those exact segments, and discards the per-Turn reference
map before TurnStore. This marker lives only in the snapshot schema, never in
writable or durable `TurnProjection`.

A terminal Turn's durable projection carries one authoritative `cost` total
beside `usage`: the lifecycle fold derives it from the accumulated usage with
the single `lib.cost` formula — the same math as the legacy done-event stamp —
and re-derives it on every projection write, so a stale carried value can
never survive next to different math. `apiRounds` remains the per-round
breakdown ledger; the finish footer and cost popover read only this top-level
total. Turns settled before the fold have no `cost`; the client-side cost
cache fills them, and that fill rides presentation state into the footer's
re-render compare so the late-landing value is not diffed away.

That same browser view content-addresses repeated terminal-Turn `toolContent` and
`results` values whose canonical JSON is at least 1 KiB. Full SHA-256 keys
address at most 256 shared documents and 4,096 references in one response;
unique, conflicting, over-budget, active-Turn, or unserializable values remain
inline. The generated schema accepts only the two declared reference fields,
full digest keys, and the bounded dictionary. Before any state publication,
the browser restores the exact shared values into shallow-copied rounds,
resolves segment references against those rounds, and discards the dictionary.
Missing documents, inline/reference conflicts, malformed keys, or a non-array
`results` value are protocol errors. Thus the wire and parse work shrink while
durable storage, the independent-client `full` response, TurnStore shape, and
replay events remain unchanged; no response cache or cross-request lifetime is
introduced.

Frozen pre-attachment Turns may still contain the same historical image twice:
one `base64` field and one data-URL `preview`. For a completed Turn with a unique
ID, positive projection revision, image index below 20, and at least 1 KiB of
recoverable encoded data under the 8 MiB binary ceiling, `refs` drops `base64`
and replaces `preview` with the generated revision-fenced Turn-image URL. Small,
active, ambiguous, malformed, or over-budget images remain inline. The
authenticated endpoint checks its owner cache scope, then the repository checks
explicit `(user, conversation, Turn, revision, index)` authority and returns
only one encoded image rather than the whole projection. A changed revision is
409; missing/foreign evidence is 404; strict base64 and magic-byte validation
fail as storage-integrity errors. Successful bytes use true PNG/JPEG/GIF/WebP
MIME, ETag, `nosniff`, and owner-partitioned private immutable caching. The scope
only partitions browser caches and never authorizes a read. Durable projections,
replay events, modern attachment refs, and the independent-client `full` view
remain byte-for-byte unchanged by this request-local compatibility projection.

New turn commands carry an already-uploaded image by its canonical
`/api/images/<filename>` URL and URL preview; the browser keeps the source
base64 only in its pending draft. Problem: copying both the original base64 and
its data-URL preview into `turn.create_pair` duplicated an image and could exceed
the storage operation's 8 MiB request bound even after upload had succeeded.
Decision: compact only confirmed local upload references when constructing the
turn command, leaving images without a durable URL inline so upload failure does
not silently discard them. Alternative rejected: raising the storage frame
bound would retain duplicate user media in every durable turn and increase
memory and replay cost. An oversized turn still returns an actionable 413;
no conversation or turn is committed by that pre-dispatch rejection.

Budget evidence from the 2026-08-28 largest-local-snapshot audit (594 Turns):
the independent-client view remained 11,382,693 bytes while `refs` measured
3,720,697 bytes (67.31% smaller) and 495,747 bytes with Brotli quality 2. A
generated-contract Node harness parsed, validated, and materialized all 594
content rows and 445 round-thinking rows in a 26.2 ms median; exact per-round
CNY totals matched the full view for all 540 cost-bearing API rounds.

Tail-window budget evidence from the same 2026-08-28 read-only database audit:
on the largest projection-byte linear sample (594 Turns), `refs` fell from
3,720,697 to 354,310 bytes (90.48%), Brotli-q2 from 495,745 to 34,181 bytes
(93.11%), and median snapshot service time from 151.82 to 22.79 ms (84.99%).
On the largest Turn-count linear sample (1,624 Turns), it fell from 7,599,898
to 358,490 bytes (95.28%), Brotli-q2 from 678,359 to 28,367 bytes (95.82%),
and service time from 196.85 to 18.98 ms (90.36%). A generated-contract Node
harness reduced JSON parse + schema validation + reference materialization on
that sample from 44.71 to 3.12 ms (93.02%), while restoring exactly 96 Turns.

Native MCP image blocks cross the executor boundary only after owner-scoped
media persistence. `TurnProjection.images` contains bounded
`TurnImageAttachment` references (`attachmentId`, owner-authorized `preview`,
MIME/provenance metadata), never raw base64. Snapshot/replay therefore drives
the same attachment block and preview action as uploaded images;
checkpoint/continue retains refs and regenerate clears stale refs before the
new attempt.

Continue is one logical Turn: new root `llmRound` and tool `roundNum` advance
from retained checkpoint chronology. Validated progress, images, file receipts
and cumulative billed usage survive; replay supplies saved results to the
model, not a second tool execution. A successor `attemptId` / `taskId` remains
an internal stale-write/receipt fence, not a visible Attempt 2. Historical
execution-local counters stay readable: presentation folds resets into
continuous Round labels without rewriting saved rows. Producer scope still
owns batch keys and exact request-inspector links; equal counters from distinct
executions are never merged. Legacy unstamped rows are frozen under the
outgoing owner and retain contiguous-occurrence compatibility.

**Problem:** resetting counters and retaining a later suffix made Continue
look like a fresh request over incompatible history. **Decision:** atomically
replace that suffix and continue the checkpoint's logical round frontier in
executor events, storage and UI. **Alternatives:** a visible A2 label merely
explains the discontinuity; removing internal execution IDs breaks zombie
fencing and exact receipt inspection. Neither provides seamless recovery.

Checkpoint settlement and resume use the same causal replay boundary as cold
history reconstruction. The anchor records both the raw prefix boundary and
the exact retained raw positions; storage validates their types, order, count,
and bounds before creating a successor attempt. Identity-free display rows and
explicitly superseded provider-attempt artifacts are transparent. Any other
identity-bearing row with an invalid ID/name/caller/argument envelope or without
exact string `toolContent` stops the prefix, so later dependent calls are not
replayed across an invented result. `status` is a verdict, not proof of
execution: exact error/rejection/abort receipts remain replayable. A malformed
resume anchor is a typed protocol error and cannot partially mutate the Turn.

When that boundary rewinds a non-empty terminal `content`/`thinking` tail
(seamless provider prefill is impossible for it), the discarded text moves
into `TurnProjection.rolledBack` — one entry per rewound attempt, bounded to
the last four, oldest first — instead of vanishing from the render. The lane
is display-only history: it is never replayed onto the model wire and never
seeded back into `content`/`thinking`. The browser projects each entry as a
keyed `rolled-back` block rendered as a collapsed dashed disclosure
(`.thinking-block.thinking-prior` / `.content-prior`), anchored where the
tail was generated: after the retained rounds, before the terminal lanes the
resumed attempt re-streams. Empty tails leave no entry, and a malformed lane
is repaired fail-closed at projection normalization.

Browser snapshot work has one in-flight owner per conversation and retains no
completed-response cache. The runtime publishes its hydration lane before it
starts work, and the coordinator publishes its snapshot flight before any
synchronous health callback or API call. A health-driven render that re-enters
`hydrate()` or `resume()` therefore joins the same request; after settlement
the lane is reclaimed. No trailing full read is needed because commits racing
the snapshot are replayed from its exact cursor. Push and BroadcastChannel
invalidations never start a snapshot: a healthy stream already owns the
ordered projection, while a missing or stale stream reopens from its durable
cursor. Browser visibility, online, push-reconnect, periodic reconciliation,
and live-attempt wake probes use the same warm-store path; only a cold store
has no cursor and needs an initial snapshot. Only a typed reset condition
crosses the authoritative snapshot boundary.

The retained sidebar-selection bridge follows the same boundary. Reopening a
warm conversation calls `wakeConversation` and resumes from the in-memory
cursor; it does not call the snapshot hydrator merely because the conversation
became active again. The typed wake owner retains the cold-store fallback, so
an absent local snapshot still hydrates once and a declared reset still
recovers authoritatively. In a frozen 2026-08-28 trace, one large conversation
produced at least 66 generated-client `tail-96` snapshots totaling 144.4 MiB
and 10.714 seconds of server work (coalesced hidden requests excluded), with a
2.188 MiB mean response. The trace includes unknown cold opens and is therefore
an opportunity envelope, not a steady-state savings claim; the executable
browser harness proves that each warm selection now performs zero hydrations
and one cursor wake while a cold selection still performs exactly one
hydration and zero wakes.

Server snapshot arrivals with the same explicit owner, conversation, and Turn
window also share one Sidecar read and stable-segment projection inside an 8 ms
gather window. Full and bounded windows are distinct authority keys and can
never leak partial state across callers. The gather closes before authority
execution, so a request arriving
after the read starts performs a newer read; no completed value or TTL remains.
The process-local registry is capped by `TOFU_STORAGE_RPC_CAPACITY` (hard
maximum 256), creates no worker pool, and fails open to a direct read at
saturation. The flight wrapper lazily builds each derived representation once;
there are at most four named views per flight and the generated browser uses
only `refs`. Full and reference callers for the same Turn window can therefore
join the same authority read while concurrent reference callers also share hashing and shallow
projection work, with no second gather delay. A later request receives a new
wrapper and rebuilds from a fresh authority read.

Each caller still receives its own HTTP response and top-level envelope so the
request-time `pushWithheld` hint cannot leak between callers; shared nested
authority/reference values are read-only during JSON serialization. Only Turns
whose terminal tool segments are safely referenceable receive shallow copied
turn/projection/segment containers; large sibling round values remain shared.
An unshared request therefore never recursively copies a conversation-sized
projection, and no flight retains a conversation after its participants return.

Generated-schema decoding compiles one bounded table of success predicates at
process import. On the normal valid path, object predicates inspect only fields
that are actually present instead of rescanning every optional contract field,
and they allocate no diagnostic paths. A failed predicate always reruns the
canonical diagnostic traversal and returns the same complete `violations`
array as before; malformed input is never accepted on a cheaper partial check.
Python and the generated browser both use JSON value equality for `const` and
`enum`, so booleans never coerce to numeric `0`/`1` across the protocol.
The compiler retains only the generated schema and predicates, never request or
conversation values.


**Problem:** tool-heavy snapshots repeat exactly-one branch checks for every
segment, and reference sizing can encode the same terminal content twice.
**Decision:** the compiler dispatches a `oneOf` directly only when acyclic
object variants all require one field with distinct string constants. It still
validates the entire selected branch. Optional, overlapping, recursive or
unsupported tags retain exhaustive exactly-one checks and unchanged diagnostics.
Reference projection sizes content only after a unique matching segment exists,
then reuses its exact UTF-8 JSON length plus the compact object wrapper. Existing
minimum-size, net-savings, ambiguity and active-Turn rules remain unchanged.
**Alternatives rejected:** TTL snapshots weaken freshness; skipping validation
weakens integrity; character counts mismeasure escaped/Unicode wire bytes.
`benchmarks/conversation_snapshot_cpu.py` compares frozen source with current
code on synthetic data only. Its component timings exclude production storage,
HTTP scheduling and durability and are not production latency certification.
After generated-schema validation, the snapshot HTTP adapter uses compact
`orjson` bytes rather than Quart's stdlib encoder. Unsupported values fail
soft to the existing `jsonify` provider. Dynamic responses are still
compressed off the serving loop by the profile-aware HTTP compression policy
owned in [`modules/infra_runtime.md`](../modules/infra_runtime.md); they do not
enter its static-artifact cache, so no conversation response is retained after
delivery.

### Lazy historical read-tool output

**Problem:** a short conversation can contain hundreds of read-tool results; Turn-count windows alone leave multi-megabyte output on the initial decode path. **Decision:** the generated snapshot and history clients opt into `toolPayload=lazy` together with `segmentPayload=refs`. Completed `read_files`, `grep_search`, `find_files`, `list_dir`, `get_conversation`, `repo_map`, `read_tool_artifact` and `search_tool_artifact` rounds whose exact string `toolContent` exceeds 4,096 characters publish `_snapshotToolResult`. The reference carries owner scope, conversation/Turn/call identity, original round index, exact projection revision, character count and at most 240 UTF-16 units of preview. Unique call identity and referenceable segment mirrors are required. Missing inline output is explicitly deferred, never an empty result. Results metadata, tool arguments, status and execution provenance remain. Proven-success `run_command`/`code_exec` rounds additionally defer structured `results[].output`: every result must have an integer zero `exitCode`, with no timeout, interruption, rejection, error, background or not-run evidence. Ambiguous/legacy-only and unsuccessful commands stay inline. Command references carry `fields: [toolContent, results]`, and the revision-fenced detail restores the exact original result array to a display clone only. One native keyboard- accessible disclosure is retained after hydration; initial exit status and command metadata never depend on expansion. This avoids duplicate structured stdout while preserving all command failure evidence. Swarm lifecycle payloads and diagnostics remain inline because text-only restoration cannot replace their identity/recovery consumers.

The exact command-restoration fixture measures 44,615 → 1,020 UTF-8 JSON bytes
(97.7% smaller); this is a synthetic transport measurement, not a live latency
claim. Native/HTTP specifications drain byte-windowed history exactly once,
retain an oversized single Turn and reject wrong-owner/stale-head reads. Real
Chromium verifies cold/warm request reuse, late-result isolation, keyboard
command hydration with computed output visibility, HTML escaping and retry/
navigation-abort behavior.
Active, resumable, failed and other specialized-tool projections stay complete;
independent clients default to the existing inline `full` tool payload.

`GET /api/v4/conversations/{conversationId}/turns/{turnId}/tool-results/{roundIndex}`
requires `projectionRevision` and `ownerScope`. The repository uses the existing
owner-scoped `turn.get`; the service verifies completion, exact revision and
round identity before validating and returning original `toolContent`. A wrong
owner/missing result returns 404; a changed revision returns 409. Responses use
`private, no-store`, so deletion or authentication changes cannot reuse an
indefinitely cached private output. Opening a mounted disclosure fetches once;
closing or retiring it aborts pending work, transient errors offer retry, and
stale errors request conversation refresh. Loaded detail belongs to the mounted
DOM block only and never writes TurnStore or a live projection patch base.

Readonly terminal projections never become a writable patch base: a forward
attempt/delta patch against a deferred Turn requests an authoritative snapshot
before advancing its revision. Active snapshots return complete output. A whole
projection edit first makes one owner-scoped `GET /turns/{turnId}` read, verifies
its projection revision and restores complete tool-round and segment lanes.
A changed revision refuses the write and publishes the latest Turn for review;
output is never persisted as a preview or missing field. This full read happens
only on explicit edit, not on navigation or expansion of every historical card.

**Alternatives rejected:** replacing output with result metadata loses original
content; cutting stored output weakens durable evidence; inferring empty output
from an omitted field hides completeness; caching snapshots by Turn revision
alone misses queue/settings/activity changes outside that projection version.
Existing burst sharing continues to read fresh authority rather than adding
such a cache. Verification lives in
`tests/conversation/test_conversation_lazy_tool_results.py` and
`tests/visual/test_visual_conversation_navigation_performance.py`.

## Atomic ordering

`storage_conversation_sync_heads` owns a monotonic sequence per
`(user_id, conversation_id)`. A mutation transaction:

1. validates owner, command identity, and projection CAS;
2. updates turn, attempt, event, and conversation revision rows;
3. allocates the next sync sequence under the same owner lock;
4. appends the compact change event;
5. commits;
6. publishes a wake only after commit acknowledgement.

Wake loss cannot lose state. Subscribers probe the durable log when connecting
and after heartbeat deadlines.

Problem: bounding the observer's one-token queue alone still permits one
thread-safe event-loop callback per commit to accumulate before the loop runs.
Decision: each subscription coalesces wake hints before scheduling, retaining
one pending callback/token until its reader consumes it. Producers never wait
for an observer read or network send. Consumption and latch release happen in
the same asyncio task so cancellation cannot strand a consumed hint. Close
still interrupts the reader and releases its exact resources once.
Alternative considered: coalescing only inside the loop callback leaves the
loop's own callback queue unbounded; dropping durable changes is unacceptable.

`benchmarks/conversation_wake.py --commits 10000` measures this lane offline
with the observer loop paused, using disposable state and Python allocation
tracing. On Python 3.12.13, a controlled before/after run scheduled 10,000 → 1
wake callbacks, with peak traced allocation 2,403,333 → 804 bytes and hint
publication 82.053 → 35.286 ms. These are instrumented microbenchmark samples,
not end-to-end conversation latency or storage-capacity acceptance results.
`test_conversation_sync_wake_budget.py` covers blocked-loop concurrent bursts,
token consumption/rearming, timeout, cancellation, and exact close cleanup.

### Hot-path event storage (2026-09-21)

Problem: `turn.append_settled` spent ~18-24 µs/op in its sync-event stage
(`ta_sync`): the event store re-parsed the just-serialized event JSON for
validation, re-read the fresh event key for a version check, built a
projection response the caller discarded, and re-validated the public turn
through `RawValue`.

Decision: the settled-append path assembles the event and response bytes by
splicing the already-encoded public turn (pinned byte-for-byte against the
derive encoding), and inserts through
`versioned_document::put_fresh_prevalidated`, which stores the byte-identical
versioned-document envelope without the re-parse, the existence read, or the
discarded response. Key freshness is the head-allocator invariant above:
concurrent same-conversation appends still fail closed through the head row's
commit-time write witness, and the offline importer restores head and events
consistently. Only a store that already violates the head invariant would see
a silent replace instead of a conflict error. `ta_sync` measures ~8 µs/op
after the change.

Alternatives rejected: (a) slim events that reference the turn document —
replay (`turn.sync.changes`), `lib/conversation_sync/service.py::changes`,
and the browser reducer (`frontend/src/core/conversation-sync.ts`) all
require the full `payload.turns` bytes on the hot path, so the payload is
contractual; (c) deferring or batching the event write — replay cursors and
read-your-writes after commit require the event in the same transaction.

## Bounded changes

The permanent turn row owns the full projection. Mutations to an existing turn
carry a revision-to-revision `projectionPatch`, never another cumulative copy.
The reducer applies a patch only when base revision, target revision, operation,
and path all validate. A missing or invalid patch triggers one authoritative
snapshot recovery.

Full projections are allowed in snapshots and bounded new-turn events. A
multi-turn graph rewrite such as compaction emits a small
`conversation.activity` event with `requiresSnapshot` and re-anchors once.

The two replay surfaces do not imply two durable AttemptEvent bodies. New
`attempt.event` change rows retain only the private attempt sequence reference;
the Sidecar reconstructs the exact public ConversationChange with one fenced
JOIN. Historical inline rows remain readable. Retention cannot delete a
referenced source; explicit turn deletion instead expires the affected change
prefix so stale cursors recover through the ordinary snapshot boundary.

`projection.attachments` contains at most 20 canonical
`TurnMediaAttachment` references. It carries display/status metadata only;
document text, video transcripts, frames, and source bytes remain in the
owner-scoped media/Knowledge authority and are resolved within a bounded model
request. Turn creation treats the browser object as untrusted, resolves every
ID under the authenticated owner, and stores the server projection. A video in
`processing` state may be committed without blocking the turn. Historical
`pdfTexts` and `videos` fields remain projection/read compatibility for old
turns, but new unified uploads write `attachments` only.

`projection.activityTimeline` is the bounded execution-history sidecar for one
Turn. Runtime task events remain the raw facts; the lifecycle folds only
durable diagnostics — tool lifecycle, retry/compaction cycles, schema
isolation, model fallback, and failures — into a maximum of 128 rows and
96 KiB of serialized JSON, coalescing repeated retry cycles and correlated
tool progress. Routine phase status text and per-round model-request
bookkeeping stay out (the live-status surface and the turn trace own those).
Attempt events carry the resulting projection patch, and snapshots carry the
same document, so the browser never subscribes to a parallel diagnostic
stream. Timeline rows are display-only projections: they are not messages,
model context, tool calls, full receipts, or execution authority. The browser anchors each
warning/error row inline at its `toolCallId` or 0-based `llmRound` (never as a
consolidated tail block). Routine info-level status, tool, and model rows are
display-filtered — the inline tool blocks and live-status surface already own
those facts. A settled `context_compaction` row is the explicit exception: it
is the projection of a durable archive receipt, not a progress beat, and exposes bounded
before/after token and message accounting without embedding archived
transcript content.

The per-generation-attempt timing document is the separate user-perceived
latency authority; `projection.timingTrace` is its terminal mirror on the
current Turn. The task event fold owns server spans and a coalesced history of
the exact live phase prompts; terminal settlement freezes them into the attempt
row and Turn mirror before reconstructible event rows can expire. The generated `recordPerception`
command appends only schema-closed browser receipt metadata (phase/terminal
paint and transport degrade/recover), idempotently by owner + attempt +
observation ID. It never accepts transcript content and never changes execution
status. The document is capped at 256 spans, 128 gaps, 128 prompt rows, 64
browser receipts, and 96 KiB with explicit dropped counts. Each receipt update
writes only the small attempt document under the same lock as terminal
settlement. It does not advance Turn/conversation revisions, emit a sync patch,
open a parallel stream, or become browser state authority. Full semantics and
cross-clock caveats are in `TURN_TRACE_CONTRACT.md`.

Proposed-plan Markdown is bounded to 64,000 characters / 256,000 worst-case
UTF-8 bytes. The Plan protocol owns three logical durable documents: tagged
source content, its `proposedPlan` sidecar, and—after acceptance—the immutable
input-turn `planExecution` handoff. It does not create the former
`task_results.meta.plan` duplicate. Ordinary task-result, segment, and turn
content mirrors predate Plan Mode and remain under the general transcript/task
retention budget; the bound here measures Plan's logical payload and added
sidecars rather than relabeling those baseline mirrors as new Plan state.

During reconnect windows, the terminal patch is exposed once in attempt replay
and once in conversation-sync replay but encoded once durably, while
execution's create-pair replay
temporarily carries the handoff once. Thus the Plan-protocol peak is bounded by
six worst-case plan texts plus small envelopes; both replay logs are TTL-pruned,
and derived turn search is separately capped at 10,000 bytes. The executable
Unicode serialization budget test is in `tests/tasks_pkg/test_plan_mode.py`; replay
retention/reclaim contracts are in `tests/storage_authority_sql/test_attempt_event_retention.py`.

## Proposed plan and execution handoff

A successfully completed Plan-mode task explicitly mints
`projection.proposedPlan` from its complete
`<proposed_plan>...</proposed_plan>` block. Generic projection normalization
never infers execution authority from ordinary assistant prose. New Plan turns
use the durable `planner` actor; an explicit compatible sidecar remains readable
for imports and retries. Its `planId` is a content hash, and consumers never
rediscover it from rendered HTML or a message index.

`planExecution` is server-authored. Ordinary create/attempt/settled inputs
cannot mint it, and a generic turn update may only preserve the exact sidecar
already stored by the server. Settled imports may carry a self-consistent
`proposedPlan` for compatibility, but never an accepted execution handoff.

Execution is a dedicated idempotent command. It must name the source turn,
source projection revision, and plan ID. The command service verifies all
three, requires the source to be the lane tail in the same atomic transaction
that creates the next input/output pair, stores a typed `planExecution`
handoff, and persists `planMode=false`. Continuing the discussion advances the
lane and therefore makes the earlier decision stale instead of executing it.
The same lane-local rule covers the main conversation and an expanded branch;
the frontend decision bar follows whichever lane currently owns the composer.
Legacy `endpointMode` / `endpointEnabled` inputs are discarded; they
do not select a live execution owner.

`contextMode=current` projects normal lane history plus the exact handoff.
`contextMode=fresh` projects only that handoff for model transcript history;
normal system/workspace constraints are still composed. Fresh execution never
deletes or rewrites durable conversation history.

## Replay and recovery

Cursors are opaque and scoped to owner/conversation. Native EventSource uses
`Last-Event-ID`. Expiry, sequence/revision gaps, malformed frames, identity
mismatches and server restart require `sync.reset_required` and a snapshot.

**Problem:** a failed reset snapshot could reopen at a newer cursor while
retaining stale state indefinitely. **Decision:** the coordinator retains a
snapshot-required barrier until required projection ingestion succeeds; reset
hints never acknowledge cursors. Hydrate, resume, wake and timed retry share
one snapshot flight. Failures back off; pause/suspension retain the barrier
without background retries. History/rank reads require an accepted snapshot.
**Rejected:** direct SSE reopening skips state; per-page retries split authority.
Read cancellation, observer isolation, watchdog and resource regression rules
live in [Defensive patterns](../DEFENSIVE_PATTERNS.md#conversation-sync-ownership).

Generated EventSource URLs carry page-scoped `streamClientId` and monotonic
`streamGeneration`. Equal-generation native reconnects and newer explicit
recoveries supersede the same client's older subscription synchronously,
waking its wait and releasing its exact token. Older generations get HTTP 204
to stop native retry. Legacy clients remain readable without replacement rights.

SSE has no per-principal count quota or shared capacity lease; new conversations
cannot evict other subscriptions. Exact-client generation replacement owns
cleanup, with bounded owner-generation tombstones carrying no projection data.
An unconsumed response has a 10-second body-start deadline. One rollback scope
covers every setup exit, including cancellation, until response construction
transfers release to the generator finalizer and that deadline.

Browser residency follows visible demand: the active shell keeps its stream;
an inactive shell keeps one only while its authoritative TurnState has pending
or running Turns. Selection/newChat explicitly re-evaluate previous/current IDs
because changing `activeConvId` emits no TurnState frame. Warm checks are O(1).
The resource test visits 32 settled conversations, requires one open source per
lease transfer and zero after disposal; see
`test_push_invalidations_do_not_reload_a_live_conversation_snapshot`.

Deletion atomically removes active header/turn/attempt/event/replay state and
moves a non-executable graph to recoverable trash. Browser disposal releases the
coordinator, store, stream, subscriptions and health entry. Restore/clone use a
fresh authoritative snapshot, never browser message arrays. The lifecycle
contract is `contracts/conversation_lifecycle_v1.yaml`.

## Dispatch handshake

Database commit and worker startup cannot be one transaction. The application
closes the gap by claiming the accepted attempt, registering an executor task
without starting it, and binding the task to the owner-scoped attempt. Bind is
durably `pending`: it means the task is accepted in the bounded local Agent
scheduler, not that CPU work has begun. The physical worker entry executes the
owner- and task-fenced `turn.attempt.start` command, which is the sole normal
`pending` -> `running` boundary; the first exact task event may perform the same
transition only as a compatibility repair. Queue refusal or worker-entry
failure terminally settles the same attempt with the complete
`task_start_failed` envelope. A typed `model_route_unavailable` startup failure
retains the selected native model and a safe route-configuration diagnosis in
the same persisted and returned envelope; retry guidance requires checking
authorization, deployment enablement and capabilities or selecting another model.
If owner-scoped durable cancellation wins before task binding, the unstarted
registry task is discarded and the command returns the cancelled attempt and
turn normally. A rejected binding is classified as cancellation only after
reading that exact attempt's durable `interrupted` state; missing attempts and other
binding failures retain the startup-failure path.

Canonical local-executor commands persist `dispatch_mode=conversation_executor`
and the server-authored request-start cancellation watermark in the same
transaction as the pending attempt. A serving-loop owner queries the partial
`pending + empty taskId + dispatch mode` index in batches of eight after a
1.5-second grace period. Only a process role with the declared task-worker
capability (`worker` or personal-mode `all`) owns this loop; API and scheduler
replicas never claim executable attempts. The owner rehydrates the durable
principal, Turn, config, and original cancellation watermark, then enters the
normal claim/bind/start path.

The watermark travels only through the dispatch cancellation port, not executor
configuration. An owner-authorized config reread must strip it again while
retaining the durable principal and accepted execution policy.
Empty `dispatch_mode` is reserved for external or manually owned lifecycle
callers and is never interpreted as permission to start billable model work;
pre-contract attempts therefore also fail closed instead of being guessed.

The one-shot claim carries a process-stable dispatch owner. The same process
may retry an ambiguously acknowledged claim, while a different owner loses the
compare-and-set. A fixed 256-stripe in-process lock keeps claim through task
registration/bind single-winner without memory growth from command IDs. The
task is still bound before spawn, so `taskId=''` remains proof that no executor
was launched and is the only state eligible for automatic dispatch recovery.
The original cancellation watermark, rather than recovery time, preserves a
Stop arriving anywhere in the request-to-commit gap.

Snapshots retain `AttemptRecord.taskId`, so a browser that reloads or
reconnects derives `pending + taskId` as “waiting for a server Agent execution
slot.” A pending attempt without a task binding is still in dispatch
preparation, and a running attempt has acquired a worker. Transient phase
frames may add detail but never redefine those durable meanings.

`activeAttemptPhase` optionally restores the latest retained event's phase for
the current running main-lane attempt at the snapshot's read head. The read
adds at most one indexed event lookup and decodes at most 32 KiB; a missing,
cleared, phase-less or oversized latest event yields no phase, with no scan
of older history. The browser accepts it only on a full snapshot with matching
Turn/attempt identity, running status and a covered projection revision.
Terminal/superseded attempts and historical timing traces never restore a
live label. Unknown phase copy does not assert transport recovery.
A command ACK may install a projection before its same-revision attempt frame. Matching current, non-superseded attempts with an unseen sequence still consume explicit `phase` (including `null`) without replaying the installed patch; older revisions, foreign attempts and consumed sequences never roll back the label.
Only operations marked `x-tofu-idempotent-retry` retry automatically. The
generated client reuses the validated request document and command ID for
ambiguous network or declared retryable failures. Abort and non-idempotent
mutations do not enter that loop.

## Health

Visible conversation heartbeat frames own `conversation-sse` health.
`connecting` and `recovering` are transient; only `degraded` and `offline`
affect the aggregate badge. Background task streams use the separate
`task-sse` transport and cannot overwrite a conversation coordinator's state.

Heartbeats and snapshots also carry `pushWithheld` (always explicit, both
ways). It is the READ-side probe of a WRITE-side delivery wedge: while the
conversation's live task has authoritative frames withheld on storage retries
(durable-before-visible, `TaskRuntime.append_event` stamps
`_pushWithheldAt`), the withheld frames themselves can never report it, so
`routes/conversation_sync_v3.py` polls
`lib.tasks_pkg.manager.runtime.push_withheld_for_conv` and marks heartbeats
`degraded` with reason `storage-write-wedged` (distinct from the read-side
`storage-read-degraded`). The browser folds the flag into `TurnState`
(snapshot fold + heartbeat action) and the live-status block presents the
honest `storage_wedged` phase label instead of the generic waiting
placeholder; any stale livePhase on record is history while the wedge lasts.
The first post-wedge heartbeat/snapshot carries explicit `false` and clears
it. Pin: `test_push_withheld_wedge_rides_snapshot_and_heartbeat` and
`test_push_withheld_wedge_replaces_the_waiting_placeholder`.

The change-page read inside the stream is replay-safe, so wedge-classified
storage failures (`database_timeout`, `database_busy`,
`database_unavailable`) degrade it to heartbeats with backoff instead of
tearing the stream down — the RPC layer marks those non-retryable only
because COMMAND commitment is ambiguous, which never applies to this query.
Before this rule (2026-09-18 incident) a wedged authority killed the ASGI
response with a 503, and every EventSource reconnect added a pre-body probe
plus a possible full snapshot refetch on top of the wedge. Genuinely
permanent failures (protocol, auth, not-found) still close the stream.
Pin: `test_sync_events_stream_degrades_on_wedge_read_failures`.

## Extending the protocol

1. Edit `contracts/conversation_sync_v3.yaml`.
2. Add or change the semantic storage operation and atomic change capture.
3. Update `ConversationTurnCommandService`; keep routes stateless.
4. Regenerate with `python3 scripts/gen_conversation_sync_contract.py`.
5. Consume only generated browser methods and types.
6. Test wrong-owner access, CAS, idempotency, atomic replay, bounded payloads,
   reset recovery, cancellation, and disposal.
7. Run generator check, TypeScript check, focused backend/browser tests, then
   the production frontend build.

Executable contracts live primarily in `tests/conversation/test_conversation_sync_v3.py`,
`tests/frontend/test_frontend_attempt_stream_vite.py`,
`tests/turn_lifecycle/test_turn_command.py`, and
`tests/storage/test_storage_authority_sql_contract.py`.

## Catalog deletion evidence

Sidebar reconciliation evicts an absent persisted shell only when the current response supplies a nonnegative safe-integer total equal to its valid, unique row count. A missing or malformed total, duplicate/invalid row, or partial page cannot prove deletion; an earlier response’s total is never reused for this decision. Local drafts remain. Confirmed deletion disposes the Turn runtime and cached metadata, then selects a remaining conversation or opens a fresh chat. `tests/conversation_sync/test_sync_digest_ghost_eviction.py` exercises the shipped catalog/invalidation adapters; `tests/conversation/test_conversation_catalog_load_runtime.py` checks response-local completeness evidence.

Queue activation/cancellation are idempotent by durable re-execution, not by
retaining the first full Turn response in the bounded command-receipt store.
After cancellation, retry reports `cancelled: false`, empty `deletedTurnIds`
and no original `inputTurn`, without another revision increment. Clients
converge from the authoritative snapshot; a retry must not fabricate a deleted
input body. The Python and native storage operations share this contract.

Catalog loading owns at most three HTTP requests per load, including any
unconditional recovery after an unusable 304. Each request has a 12-second
budget covering headers and JSON-body reading, and at most one live deadline
timer. 503 backoff is capped at five seconds and never runs after the final
request. Disposal aborts the active fetch, clears the backoff/deadline timer,
releases single-flight waiters even if a port ignores cancellation, and blocks
late catalog/cache publication. The typed loader owns these timers; the
retained adapter forwards its signal without adding another timeout owner.
`tests/conversation/test_conversation_catalog_lifetime.py` fault-injects stalled requests,
stalled JSON, oversized retry headers, disposal and 304 recovery budgets.

## Authoring-round reasoning evidence

`TurnLastRoundUsage.reasoningResolution` is an optional bounded
`TurnReasoningResolution` compiled by the selected execution slot. It carries
requested mode/value, effective mode, source/reason, and the actual native
control mode/value. It contains no prompt, provider credentials, or model
catalog snapshot. The same `_reasoning_resolution` remains in retained authoring
API-round usage. Discarded billing calls never replace the authoring snapshot;
a later authoring call without evidence clears any old evidence. Existing Turn
projection persistence carries this field without a new storage lane.

The Finish Tag displays mappings from this evidence, not from current model
metadata. Legacy projections may omit it; absence does not authorize an actual
effort claim. See [rendering contract](../RENDER_CONTRACT.md#finish-tag-reasoning-mapping).

## Conversation-owned automatic workspaces

**Problem:** Studio/project setup and navigation restoration made filesystem
capability depend on a mutable UI environment. **Decision:**
`lib/conversation_sync/workspace.py` resolves explicit chat intent before Turn
acceptance and retry acceptance; existing Attempt config is the sole durable
execution snapshot. Empty chat bindings get owner/conversation-scoped private
roots under the writable runtime data root. Explicit scalar/list-only selections,
read-only references and remote bindings remain explicit; no recent-project,
server-launch-directory or other-conversation inference is allowed. Reserved
workspace/origin fields in the execution-identity contract are server-authored.
SDK/embed/headless execution does not enter this application policy.

Physical worker admission creates a private root idempotently with no-follow
path checks. Managed roots never adopt a containing server Git repository;
commands fence upward Git discovery. Non-Git as well as Git tasks own frozen
root policies, independently of cache eviction, navigation and draft state.
Child/background execution borrows that policy, not publication ownership.
Private roots are durable user outputs: normal terminal cleanup must not delete
them. They are not persisted as user-selected conversation attachments. Creation
is lazy at worker admission, with no new idle worker, scan or workspace store.

**Alternatives rejected:** a second binding database duplicates Attempt authority;
unconditional kernel scratch allocation changes SDK contracts; guessing the last
active project risks foreign writes. Filesystem isolation remains cooperative,
not an opaque-program sandbox. Narrow tests cover accepted/replayed bindings,
SDK pass-through, owner/conversation separation, symlink refusal and cache loss:
`test_automatic_workspace.py`, `test_conversation_commands_v3.py` and the
workspace browser journey.

Resource/lifecycle evidence (2026-10-10, synthetic Linux temporary filesystem):
2,500 pure resolutions measured median **11.95 µs**, p95 **12.75 µs**; 100
worker admissions measured median **95.91 µs** first creation and **84.87 µs**
reuse. These are policy/local-filesystem measurements, not FUSE or production
latency guarantees. Resolution performs no filesystem I/O; provisioning adds
one leaf directory per used owner/conversation, no files, idle workers or
recursive scans. Output files are durable user state, not cache: no TTL or
terminal cleanup reclaims them. Existing task teardown releases root-policy
projections; the accepted Attempt retains the binding needed for recovery.


## Exact input-round directory and rank navigation

`GET /api/v4/conversations/{conversationId}/turns/rounds` requires an explicit
`laneId` and `syncSeq`. Owner comes only from authenticated scope. The response
carries `ownerId`, `conversationId`, `laneId`, `syncSeq`, exact `totalRounds`,
zero-based `offset`, and up to 100 compact entries (`turnId`, `ordinal`, `actor`,
1-based `roundNumber`). `anchorTurnId` optionally resolves the preceding/current
input rank for viewport selection. `nextOffset` pages the directory; rank N is
read with `offset=N-1&limit=1`. Stale heads return `stale: true` and no entries;
their zero total is not authoritative and must never be displayed as a count.

Inputs are exactly `human`, `critic`, and `virtual_user` in lane ordinal order.
Assistant-only messages, arbitrary assistant/input ratios, and ordinal gaps do
not create input rounds. SQL uses actor/ordinal columns; native scans existing
compact lane heads in bounded pages, retaining only the requested entries.
Neither implementation reads projections to obtain a total. There is no new
persistent counter or write-path-dependent count maintenance.

The browser directory is instance/owner/lane scoped and read against the one
coordinator's committed head. Snapshot resets, input upserts, and deletions
invalidate rank selection; pure projection patches do not change actor ranks.
Rank targets are revalidated under the current head before jumping. Bodies use
the same coordinator/TurnStore path, with at most two detached pages (eight
nearby Turns plus a one-Turn fallback). Detached ingestion never advances the
continuous tail history cursor, and never counts historical hydration as a new
Turn. Only a page whose `beforeOrdinal` equals the continuous frontier may
advance that frontier, so a loaded island cannot skip unseen history.

### Native legacy compact-head upgrade

New writes store immutable `actor` in the existing compaction head, through its
shared head writer. A legacy head lacking only `actor` resolves that field from
the canonical owner-scoped Turn document in the same read snapshot. Navigation
never hydrates external projection payloads, writes metadata, advances sync
heads, or infers actors from alternating positions. Its existing deadline and
per-document bounds remain in force; only legacy heads incur document reads.
Identity (conversation, Turn, lane and ordinal) and every remaining compact
field must match the source exactly. Missing sources, malformed actor fields
and mismatched revisions still fail closed.

Problem: requiring an offline backfill before navigation made valid existing
conversations return HTTP 500 while their authority was live. Decision: a
read-only, exact legacy adapter at the existing directory owner. Alternatives:
blindly estimating actors corrupts round ranks; live backfill inside a query
violates read authority; full projection hydration copies unnecessary bodies.
The optional exclusive offline `migration-session` operation
`migration.turn.round_heads.upgrade` remains available separately per tenant/owner
to remove the legacy-read cost. Start with `{"cursor": null, "limit": 16}`,
continue with the returned cursor until null (maximum page 32). It is idempotent
and preserves Turn/projection revisions, timestamps and conversation sync heads.
SQL stores already expose the actor column. Tests live in
`packages/tofu-db/src/turn_byte_window_tests.rs` and run as `byte_window_tests`.

Turn image and media attachment arrays have no private count admission ceiling.
Generated validators derive this from the canonical schema. Owner-scoped media
resolution processes the complete list, deduplicates identities and replaces
client metadata with canonical refs before retention; each item's existing
shape and ownership checks remain. See [attachment lists](../modules/ingest_media.md#complete-turn-attachment-lists).
