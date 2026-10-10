# Context compaction policy
This guide owns the detailed compaction policy referenced by the
[context-engineering domain map](modules/context_engineering.md).
Automatic compaction bounds the request view without rewriting durable Turns. Working-state summaries retain known repository/root attribution for paths as rootname-relative or original absolute paths. A new task worktree is not evidence that every historical path moved. Unknown attribution stays unresolved; fresh environment mappings govern execution.
Manual compaction writes a persistent summary boundary through the conversation
command service, guarded against intervening Turns. Both share summarization
and token-budget primitives, never persistence semantics.
L1 receives an API-form working projection and replaces only cold tool/image
bulk; it performs no conversation read, projection update or change notification.
Completed ordinary chat turns publish this bounded view through the owner-scoped
durable continuation repository. The next turn validates it and transforms only
uncovered Turns; restarts/cache eviction do not resurrect summarized history.
Missing/invalid checkpoints rebuild unchanged Turns and pass fresh admission.
**Continuation boundary:** display updates previously invalidated summaries, while shared eligibility wrongly blocked completed Continue publication. Decision: semantic proof v2 preserves typed content/tool/Attempt/policy/media fences; Continue proves its input baseline, retains fresh suffixes, and publishes only verified complete model content. Rejected: weaker witnesses, history deletion or higher thresholds. Old proofs rebuild once with explicit reasons. Details: `contracts/model_context_v1.schema.json` and [storage operations](STORAGE_OPERATIONS.md#model-context-semantic-version).
Ordinary `excludeLast` excludes the empty output target, not continuation itself:
validate that boundary. Actual resume payloads, fresh execution boundaries and
changed historical witnesses invalidate continuation.
L1 indexes diagnostic/pairing/newest-unconsumed positions once per pass; input-bounded
references expire on return. Reverse selection, occurrence IDs and consumption stay intact.
Previews inspect at most 121 characters for two-line/120-character output.
`tool_compacted` stays durable-before-visible without tool-row mutations; DEBUG success
logs and content-free aggregates report emitted/failed counts and monotonic durations.
Pipeline timings distinguish L1, L2 (checks/provider/persistence), recomposition
(including analytics) and advanced work. `done` means normal return, not adoption.
L1 total includes indexing/counting beyond event time; timing creates no authority.
Summary formatting counts once; centre-first `O(n log n)` elision uses constant-time
length deltas, preserves human text and reports irreducible overflow honestly.
### Shared child invocation ownership
`compaction/_ownership.py::compaction_owner_key` is the working-state identity
boundary. Root/direct callers retain their existing conversation keys. A child
identified by `is_child_invocation(task)` uses
`invocation:{user_id}:{task_id}`; its parent `convId` remains an authorization
link, never its compaction cache or accounting bucket. Explicit legacy `conv_id`
arguments cannot override child identity. Missing child task identity fails closed.
L1/advanced prefix reads, L2 admission/cooldowns, reactive invalidation, summary
affinity and usage, archive creation/retention and prior-receipt lookup share this
key. Real compaction and canonical call-level usage/receipts remain enabled.
Children do not re-pin the parent's durable objective from generated summaries.
No new ledger, store or worker is introduced. Child success and fatal/cancel
handlers drain summary costs through `_settlement.py` before terminal delivery;
the drain is idempotent and retains call-level costs. Provider token/cache writes
and final cache cleanup use the same owner key; teardown releases only the owning
child's cache and cooldown entries (see the lifecycle contract below).
Storage remains the existing `ConversationStore` authority. A task-scoped key is
**not** a durable conversation ID: child invocation integration must supply an
owner-authorized task archive scope through that repository boundary, not create
a synthetic conversation or fall back to the parent's archives. The default
conversation-only adapter alone is insufficient for child receipt durability.
`tests/tasks_pkg/test_child_compaction_ownership.py` specifies sibling/root isolation,
canonical costs, cooldowns and create/read/prune scope parity with isolated fakes.
Final recovery reuses that scope; metadata failure never falls back to parent archives.
These specs cover usage settlement, not real Sidecar scope or provider cache cleanup.
### Assistant phase and task continuity
The default operating prompt interprets action requests as scoped execution,
preserves authorization from the conversation, and carries the original task
through status questions and compaction. It asks for essential missing input
or new authorization after completing independent authorized preparation.
Explanation-only requests, tool permissions and destructive-action boundaries
remain explicit. The prompt is still composer-owned and cache-stable.
Single-agent Responses assistant messages retain each original `commentary` /
`final_answer` phase and message boundary in the existing `_responses_items`
sidecar, shared by live, tool-round and durable history reconstruction. Adjacent
assistant rows carrying native state are not merged. Every Responses route can
replay these messages; model-specific opaque-item feature gates are unchanged.
Replay requires matching current canonical prose, so redaction, hooks and
compaction cannot resurrect stale captured text. User messages never gain a
phase. Native multi-agent messages retain their separate attribution policy.
This reconstructible sidecar adds no store or worker. Capture is bounded to
64 messages, 64 content parts per message and 256 KiB of UTF-8 text per response;
overflow or unsupported message shapes omit phase replay while retaining the
ordinary transcript text. Existing continuation/Turn storage bounds and
lifecycles still apply. Regression and budget evidence lives in
`tests/llm/test_responses_message_phase.py`; terminal handling is specified in
[TURN_SETTLEMENT.md](contracts/TURN_SETTLEMENT.md).
### Authoritative checklist restoration
`context_composer/checklist.py` owns the model projection of the task's accepted
checklist state. It is required execution data, not optional todo guidance: it
remains available in append/replace prompt modes and when optional guidance or
a tool surface is disabled, without granting permission to call a tool.
Initial composition, final dispatch admission (before both measurements), and
reactive retry body preparation use that same projection. A summary, failed
compaction attempt, or deterministic byte recovery cannot leave the next
attempt with only a prose plan and no accepted snapshot.
The dedicated, stable `active_checklist` block carries the accepted flat list,
its exact content/status and optional explanation, including an explicitly
empty accepted snapshot so older history cannot resurrect cleared work. There
are no model-facing item IDs, revisions, ancestor paths or replan operations.
It has no lossy block-local truncation and cannot compete with optional file
reminders. Final request counting includes the complete envelope; irreducible
overflow uses the existing typed refusal rather than dispatching a partial
snapshot. New writes are bounded to 24 items × 512-character steps plus a
2,048-character explanation; legacy checkpoint migration may preserve up to
144 rows. This projection preserves context, not a completion-enforcement gate.
A complete current recovery carrier or paired direct/gateway accepted todo result (including load/lookup) proves visibility without opaque markers; later incomplete/failed receipts cannot invalidate it.
Only stale owned recovery sections are withdrawn; dialogue/results/audit history stay intact. New carriers are standalone; mixed legacy cleanup is copy-on-write.
L1/advanced/L2/reactive compaction carries state before budget/adoption checks; rejected candidates cannot mutate live messages/task/notes. Truncation counts and protects required state, or leaves overflow for refusal.
**Problem:** update-driven recovery duplicates visible progress. **Decision:** restore only missing state, at most one current carrier; preserve accepted items verbatim and label the separate explanation field's origin.
**Rejected:** per-round throttles and append-only recovery still duplicate state; hiding cards hides bad input. Specs: `tests/goals/test_checklist_context.py`.
### Provider-native compaction
Provider-native automatic compaction is a third execution location, not a third
transcript authority. Verified public GPT-5.6 Responses and supported direct
Anthropic Messages routes use the resolved economic working set as their
rendered-input server trigger. The fallback is 128K; a declared price increase
selects 90% of the preceding cheaper tier. Local L1 still removes
reconstructible bulk from the current request; local lossy L2 is
reserved for the hard-window/reactive fallback. Codex and Claude Code
subscription OAuth, generic compatible gateways, mixed provider pools and
Responses native Multi-agent rounds keep local L2 authoritative because their
wire contract does not expose the corresponding public compaction field.
Local L2 summary dispatch has its own stable, opaque cache/routing identity
derived from validated owner + conversation, bound to both dispatch affinity
and the prebuilt stream body; every exit restores the parent affinity, so
summary writes reuse their own prefix without delaying or overwriting ordinary
conversation settle state. Those entries share the existing one-hour TTL and
4,096-entry hard caps and add no worker, queue, or unbounded owner state.
Missing conversation identity keeps the legacy no-affinity path; an invalid
explicit owner fails closed.
### Durable adoption and Continue

**Problem:** a long archive exceeded native ordinary literal limits; adopting a summary after failed archival left Continue rebuilding cold history and hid the compaction marker. Startup compaction also lacked a zero-tool-prefix witness. **Decision:** native archive bodies use their declared 64 MiB blob budget and a compact, digest-bound outbox; ordinary literals keep the 8 MiB budget. For a Turn-bound task, the accepted post view and receipt commit before model-view adoption. Create/update failure retains the original input, emits an explicit failed compaction fact without an archive link, and stops provider dispatch. The pending compacting phase stays visible before an archive exists. An empty tool prefix is a valid startup witness. A closed suspended task saves its bounded working view through the same owner-scoped archive repository, including delivered background context and exact tool receipts. This checkpoint retains the original audit snapshot, makes no model call, emits no fictitious compaction, and follows the existing archive retention limit. Owner, Turn, history, policy, media and complete tool-prefix checks still fence restoration; changed or unproved input rebuilds rather than weakening those fences. Current logical-Turn loaded Skill batches remain verbatim through L1/L2; older Turns remain eligible for normal compaction. **Alternatives rejected:** raising all native literal limits broadens unrelated operations; adopting unsaved summaries breaks recovery; hiding pending or failed work makes repeated summarization invisible; relying on a tool-count watermark alone loses late background evidence. Specs: `test_continue_working_context.py`, the cross-backend large archive/restart producer test, and `test_visual_continue_compaction.py`.

Accepted L2 work emits `phase=compacting` before summary I/O; declined preflights stay silent and archive completion retains its durable boundary.
A summary generated inside a live task turn shares the turn's abort signal:
when the task dict carries the runtime `aborted` flag, generation streams
even without a UI delta callback so the transport watchdog interrupts it
mid-flight, an already-aborted task skips the paid call entirely, and an
abort never falls through to the text-tier retry. Synthetic task dicts
(manual /compact, standalone callers) keep the non-streaming dispatch path
unwired.
Default task-owned summaries use cost-first, highest-AA routing; scope, evidence, opt-out and ranking semantics live in [AA-cheapest compression-first routing](CONTEXT_BUDGET_POLICY.md#aa-cheapest-compression-first-routing).
### Complete summaries without a wall-clock deadline
**Problem.** Short timers can discard useful slow work or trigger lossy recovery;
clipped or provider-truncated summaries cannot safely replace source context.
**Decision.** Elapsed time is telemetry: legacy `summaryBudgetSeconds` values
(including zero) impose no deadline. Owner Stop, transport failures and bounded
429 retries remain effective, without a detached worker. Failed identical requests
latch against repeated cost; changed evidence/route changes the request digest.
Completed late output is accepted normally.
Only nonempty provider-verified normal completions can become candidates.
Length-limit, filtered, tool-call and unverified endings are rejected, including
nonstream calls. Complete output is never clipped to the nominal summary target.
Critical evidence validation precedes replacement; the recent tail is not silently
truncated to make candidates fit. Live/transient failure preserves source messages.
Mandatory admission refuses overfull prompts rather than using partial summaries
or head deletion. Explicit utility recovery remains separate; complete candidates
can still fail fidelity, reduction or optional economics checks.
Preflight and dispatch share output-capacity headroom, separate from priced output.
Preflight never prices output as input; dispatch revalidates the exact prompt and
refuses spending if its route changes or its quote exceeds the approved allowance.

Usage is recorded even for rejected/failed attempts. Receipts distinguish generation
from adoption, show measured route capacity/prompt/reserves/economic target, and identify
the summary route/API-equivalent USD estimate. Unknown usage/prices never mean savings.
Logs expose identity, reasoning length and first-text latency, never reasoning content.
**Rejected.** A 60/120-second deadline, clipping, pricing-as-capacity or destructive
live recovery improves apparent latency at the expense of state. Executable specs:
`tests/tasks_pkg/test_live_summary_budget.py`, `tests/tasks_pkg/test_transient_summary_budget.py`,
`tests/compaction/test_summary_completion_policy.py`.
Opaque reasoning is never locally tokenized as if ciphertext length were model
tokens. The counter starts from the previous provider-measured input, estimates
the visible appended suffix, and adds the provider-reported hidden-thinking
count once when the suffix actually replays encrypted Responses reasoning or
Claude `redacted_thinking`. The next successful provider input measurement then
replaces that reserve. For Anthropic native compaction, billed aggregate usage
and effective post-compaction prompt size remain separate fields so cost and
context safety do not borrow each other's semantics.
Turn-native manual compaction persists one canonical public `compaction` block
through the Sidecar operation. Private runtime markers are never transcript
authority; the read-only legacy projection adapter reconstructs the v1 marker
fields for old consumers without a second stored representation.
The first real user anchor and required system/project policy remain available
after compaction. Tool-call/result pairing is derived only from the adjacent
assistant/result run; duplicate IDs pair by occurrence. Orphans remain
unpaired, so a later success can never authorize or relabel an earlier call.
L1's ordinary tool-result hot tail is the intersection of a 40-result ceiling
and a 48K estimated-token ceiling. Its ordinary pass never edits the warm cache prefix and
always protects the newest complete tool-call batch, even when that batch alone
exceeds 48K. Older reconstructible results become request-local placeholders;
their durable tool results and artifact references remain untouched. The opt-in
`adaptive_hot_tail` experiment is not a second default authority.
Assistant tool-call turns omit empty `content` (`''`, JSON `null`, `[]`,
whitespace-only) at `_strip_empty_text_blocks` and again in cache Phase 0.5.
Present vs absent `content` is canonical-invisible but byte-divergent, so it
rewrites the GLM/Anthropic prefix cache every round. Specs:
`tests/llm_sanitize/test_empty_text_block_wire.py`,
`tests/cache/test_cache_content_wrap_invariance.py`.
Image-bearing placeholders name the durable recovery reference when one
exists: retained owner-scoped media (`tofu-media:<id>`) is rewritten to the
`att_media_<id>` form that `inspect_image` resolves through the attachment
authority, so a compacted screenshot stays re-inspectable instead of
degrading to "re-call tool". The fallback wording is kept only when no
durable reference exists. Text lanes never serialize a screenshot envelope:
`budget_tool_result_v2` and programmatic result capture degrade a
`__screenshot__` dict to its text fallback plus the same durable reference,
so raw base64 can no longer spill into paged artifacts.
### Historical evidence, live requests and execution continuity
`compaction/_history.py` owns human-source classification and historical user
carriers. A provider's `role=user` alone does not prove a human request: runtime
meta blocks, virtual-user directives and ordinary inbox notifications are not
human steering; explicit operator/human steering remains eligible. The opening
request survives as **historical evidence**, not as the default current ask.
Earlier selected requests from the folded region use one bounded policy across
repeated compactions: newest-first under 4,096 estimated tokens and eight
messages, then chronological rendering, with the opener separately protected.
Historical users in protected execution shells have a separate preservation
allowance; the receipt's retained-user count includes both regions. Full
request accounting includes every carrier and its wrapper. Selected strings
retain full text and boundary whitespace, including distinct equal-text
occurrences; multimodal content blocks remain intact.
Unselected originals remain in the transcript/pre-compaction archive.
Request-local carriers use producer-owned `_retainedUserHistory` metadata for
reselection and visible `<retained_user_messages>` attribution for providers
that strip private fields. The version remains private metadata, not prompt
prose. Legacy trusted carriers remain readable; pasted user XML never proves
producer identity. Adjacent trusted carriers coalesce into one historical
region without crossing human/execution boundaries or deduplicating equal text.
One full explanation appears in the first carrier of the rebuilt projection;
other carriers retain balanced tags and per-entry historical labels, not another
copy of the long guidance. Surviving assistant/tool rows remain in causal order.
Emergency image stripping also updates request-local typed history; rebuilding cannot restore stripped payloads (`tests/compaction/test_compaction_history_image_recovery.py`).
The explanation marks history as non-live, later human steering as controlling, and existing scoped authorization as preserved without new approvals.
Automatic L2 and manual `/compact` select one chronological complete tool-round
suffix **across user turns**, not a fresh allowance per assistant. The count is
a maximum under the preservation-token budget; enclosing user/assistant shells
are charged too. The newest complete real execution batch survives even when
it exceeds that budget. Synthetic receipts cannot displace real execution;
incomplete rounds are not relabelled complete. Emergency whole-message removal
protects the latest human and real batch; oversized irreducible carriers cause
admission refusal. Single-result byte recovery may shorten reconstructible
execution text with an explicit elision marker, never the required handoff.
The receipt is fallible working state: objective, constraints, verified work,
current state, blockers and next steps. Its **Objective** is model-authored from
verbatim human evidence; summary-input elision removes non-human prose, not
human text. A missing/blank Objective stays unresolved, never falls back to the
opening request. Continue/no-ask instructions retain their scope, later stops
or replacements win, and assistant proposals cannot become approval gates.
The advisory autopilot pin is updated only from an accepted receipt. Manual
compaction freezes its read set before summary generation, reconciles unchanged
folded prefixes with concurrent tails, and suppresses repinning if the accepted
human read set changed. This is not a new authorization store.
**Ruled Out / Dead Ends** retains verified negative conclusions with proof and
retry boundaries; pending steps cannot re-propose them without superseding
evidence. If all routes are ruled out, report the blocker. The newest typed
handoff, complete adjacent legacy `context_compact` pair, or manual summary is
carried forward under a bounded budget; chronology, not format, chooses it.
Automatic L2 also supplies the latest completed assistant wrap-up from the full source.
`_layer2/_summary_evidence.py` supplies adjacent occurrence-local tool observations:
newest 256 messages, at most 64 calls/message, 16 rows, 4 KiB/row and 24 KiB total.
Parsing caps at 32,768 characters; non-text/encoded bodies are omitted, previews
use bounded head/tail samples, and oversized identities are omitted whole.
All wrappers/observations enter the existing summary-input budget; human text
is never silently elided. Calls express intent, missing/partial results cannot
prove completion, and omitted observations are not verified negative evidence.
The prior receipt occupies one slot; default summaries never extract a duplicate ledger.
Opt-in `evidenceLedger`/adaptive validation retain the separate runtime-ledger contract,
not this default extraction bound. All are fallible evidence; newer execution wins.
Specs: `tests/compaction/test_compaction_summary_evidence.py`. No new store, worker or I/O.
Without a recognized live receipt, load only the newest owner-scoped archive
summary metadata, never the archived transcript. Text-only route switches
retain compacted state and its private provenance if non-route policy matches;
v2 `modelRef`, preferred provider and routing are route identity.
Manual compaction keeps selected human Turns/identities, never fabricated users.
Reload groups consecutive users before the summary into one historical carrier;
recent exchanges remain ordinary dialogue. CAS/archives use raw Turn space,
never indices from expanded model messages.
The receipt targets 800–1,600 tokens with an advisory 2,200-token size budget;
reconstructible `data/tool-results` paths never become durable working files.
Tofu adopts summary-last placement, not Codex `compact.rs`'s full rewrite:
execution tail, typed provenance, immutable Turns and append-only context survive.
### Replaying the model request

Checked Continue reuse and final-admission recovery follow [the coordinated budget contract](CONTEXT_BUDGET_POLICY.md#dispatch-recovery).

Compaction does **not** reduce every request to system + summary. Local automatic
L2 keeps required system/context, attributed historical users, a bounded complete
execution tail, the current human request, and one **runtime continuation handoff**.
`compaction/_handoff.py` owns request-local `_compactionHandoff` metadata
([machine schema](../contracts/compaction_handoff_v1.schema.json)) and placement. Its user role is transport, not human
authorship. Visible attribution says fallible state, not a new request, execution,
permission or completion proof; private metadata never duplicates the body.
The automatic carrier deliberately omits the manual `_isCompactionSummary` flag.

| Source boundary | Model-facing order after local restoration |
|---|---|
| Pre-turn: no real execution after the latest human input | retained history/execution → handoff → latest human → runtime context |
| Mid-turn: execution already follows the latest human input | latest human and retained execution in causal order → restored runtime context → handoff |

Phase derives **before folding** from source chronology, not API round, trigger
or fold `mode`. Legacy synthetic pairs and runtime attachments are not execution.
A late human correction/Stop starts a pre-turn boundary; a surviving mid-turn
handoff retains its phase. Open batches and orphan results defer lossy compaction
without paid summaries or mutation; legacy identity is positional, never borrowed IDs/objects.
The accepted candidate counts its complete handoff. Only an adjacent
composer-only suffix may move before a still-unconsumed mid-turn handoff; never
cross later human, assistant, tool or inbox events. After execution resumes,
positions stay stable. Repeats replace rather than nest handoffs. Cancellation
blocks adoption/fallback; existing pre-archives close as cancelled without a
post snapshot or accepted summary. Already-adopted receipts remain historical.
Pipeline/hooks/steps check Stop before starting later work; composer acquisition
checks again before adoption. Cancellation is cooperative, not rollback of earlier
accepted changes (`tests/compaction/test_compaction_cancellation_boundaries.py`).

Old complete pairs/manual summaries remain readable; automatic compaction invents no tool execution. Archive `continuation.format=context_handoff`, `phase` and
`placement` record the model boundary; `roundNum`/event locates the operation in the UI timeline. These fields are not task or authorization authority. Provider
adaptation is a separate boundary: Chat Completions uses `messages`; Responses translates calls/results into `input` items; Anthropic uses
`tool_use`/`tool_result` blocks and a separate system field. Adjacent user rows may merge, but live text remains outside the visibly closed historical envelope.
Private marker fields are not assumed to survive onto the wire. Manual `/compact` uses its persistent assistant summary boundary rather than the automatic
handoff.
`python -B -m tests.compaction.test_compaction_history_wire` emits complete JSON through
real local compaction/`build_body`: a **synthetic offline fixture**; no model or database is called.
`test_compaction_history_providers.py` and `test_compaction_handoff.py` cover Chat,
Responses default/Codex and Anthropic, both phases, continue/no-ask/Stop, repeats,
recomposition, portable replay, cancellation, pairing and irreducible recovery.
An archive proves its recorded input/receipt, not dispatch. Label reconstructions.
Inspector `messages_snapshot` with `kind=request` mirrors prepared messages;
`snapshot_delta.rebuild_snapshots` restores dictionaries and reports degraded
missing/mismatched baselines. `kind=state` is a post-tool/final view, not a request.
Even sound mirrors strip diagnostic base64/select parameters, not all HTTP bytes.
### Inspecting the compaction result
`compaction-msg-list` defaults to the saved post view in original order and
navigates to its summary/recovery row without duplication. Role and producer
provenance are separate; one linear pass checks complete legacy batches, never quoted XML.
Copy/download share this projection and distinguish unavailable (`null`) from empty (`[]`).
The [archive API](contracts/API_CONTRACT.md) explicitly labels this capture boundary;
legacy missing snapshots are not reconstructed from current messages.

```text
request-local messages → L2 / final recovery result → saved postMessages → drawer
                                                ↘ runtime recomposition
                                                  → admission → provider body
```
Automatic/recovery snapshots use working-message space. Manual `/compact`
snapshots use the accepted durable-Turn projection (including its assistant
summary marker), not a fabricated automatic tool pair. Native `system` and
`developer` rows are kept outside local lossy-summary input, including
interleaved rows; manual retention and cold rebuilding preserve explicit policies and original durable Turn order (`tests/compaction/test_compaction_policy_reload.py`).
`tests/server/test_visual_compaction_viewer.py` exercises the built lazy drawer
against local HTTP routes with synthetic owner-scoped archives; no provider calls or live-conversation mutation.
The trusted composer-owned `platform_static` now occupies a dedicated native
system prefix, preserved by local L2. Dynamic user-role carriers are separate:
the composer restores missing runtime blocks after compaction and reuses
unchanged surviving ones. Only trusted legacy static envelopes migrate; quoted
user/tool XML and old archives never acquire new policy roles. The saved result therefore excludes
that later recomposition and provider adaptation, and proves no dispatch.
Snapshots passed to storage are detached from subsequent nested mutations.
Reactive recovery refreshes saved result/counts after trimming without erasing an accepted summary.
Emergency removal protects the newest typed/manual/complete legacy continuation,
required policy, historical/latest human text and the newest real execution batch.
Provider-overflow retry uses the ordinary composer restoration owner; memory-only
recovery makes no provider reads and preserves the newest required block versions,
including instance-qualified identities. Both restore the accepted checklist.
Required carriers are never silently sliced; reconstructible result text/images
have marked byte recovery. Irreducible content forces admission refusal.
Provider adapters preserve explicit system/developer policies: Anthropic
projects both into its supported system envelope in source order; Responses
native compaction retains both before pruning old dynamic history. User/tool
text is never promoted merely because it contains an instruction-like label.
Proactive cache economics include a conservative summary-call estimate before
dispatch. Current and candidate prompt prices are resolved independently, so a
provider-declared context-tier change affects the calculation without a model
special case. Recurring savings compare the observed warm prefix before and
after the rewrite; uncached tail tokens are not assumed to become cache reads.
Once a summary has been generated, that cost is sunk and adoption compares only
the future prefix rewrite with future input savings. Archive and UI token
counters are explicitly heuristic (`tokenCountKind=estimated`), not
provider-billed usage.
`tokensBefore`/`tokensAfter` (including `.compaction-token-stage.is-after`)
count the complete message lists at the saved compaction boundary: policies,
history carriers, preserved execution, complete handoff and live input. They are
not summary-token counts, nor final wire totals with schema/protocol overhead.
Compaction counts/reduction are archive/event metadata only; newly rendered
model summaries contain no self-referential token-statistics paragraph.
Economic preflight and adoption reserve the estimated bytes-as-tokens of
removed managed carriers, so later restoration is not sold as permanent savings.
This no-I/O reserve can overcount old versions; it is not an exact prediction of
changed future provider blocks. The receipt exposes `restoredContextReserveTokens`
and `candidateMeasurementScope=compaction_result_plus_restore_reserve`.
Specs: `tests/compaction/test_compaction_context_boundaries.py`.

Cache economics may defer an optional rewrite, never mandatory input admission
or hard-window recovery. Historical `lastTurnCacheRead` is display metadata,
not evidence that a new task or a changed model/provider has a warm prefix.
Economic decisions require current-task, route-compatible cache observations.
The working-set target is economic; a cache-negative/low-yield decline below window safety stays declined at dispatch.
Proactive safety recovery and final guard share a route-derived window-safety ceiling on every round. Full prompt measurement includes tools; explicit requested output is reserved. There is no separate 256K first-dispatch cap. Astra's maintained capacity is 1,050,000 tokens; its 272,000-token pricing tier is not an admission limit or a proven universal quality sweet spot.
Route propagation and coordinated L1/L2 adoption: [CONTEXT_BUDGET_POLICY.md](CONTEXT_BUDGET_POLICY.md).
Receipts distinguish a local economic decline from an attempted summary failure;
locally measured token counts are never labelled upstream-reported usage.
The local token authority reuses exact-ish counts above 4 KiB, or above 512
characters when a caller proves reuse. Full repeated requests batch all short
text plus unique cold misses once, while stable large-prefix digest hits skip
BPE work. Encoding/length/SHA-256 keys retain no prompt text; capacity is
launch-probed with a 4,096-entry ceiling. General short text bypasses hashing;
changed content or encoding is recounted. Provider-reported per-conversation
anchors use a separate launch-probed LRU (128 fallback, 4,096 distributed,
8,192 hard ceiling): TTL/capacity/pressure eviction falls through to the next
counter, retained fields are bounded, and expiration plus replacement are
atomic.
Child invocations use `compaction_owner_key(task)` for provider-usage cache
writes and native-compaction invalidation; parent `convId` is authorization
lineage, never the child's cache key. The common settlement helper folds paid
summary calls into `usage` and `apiRounds` before child/root terminal branching
(and is shared by failure/cancellation). Drained costs are retained on the task
until projection succeeds, so a cleanup fault cannot silently discard them.
Child teardown independently releases token-cache and summary-cooldown entries,
including after `KeyboardInterrupt`, without clearing parent or sibling state.
Completed children retain no global usage/cache/cooldown scope; durable archive
retention remains the storage owner's separate lifecycle. Specs:
`tests/tasks_pkg/test_child_compaction_lifecycle.py`.
L2's request gate counts tools; its message-only estimate retains retry/archive/analytics contracts. Final post-injection admission treats that canonical complete-request
count as its total exactly once and passes it call-locally to the body clamp; v2
schema/message fields only decompose it. Missing or invalid evidence rescans.
Mutation invalidates reuse; current-task cache evidence wins after two misses.
Within the request gate, the broad heuristic prefilter is lazy. A usage-cache
hit or successful local tokenizer returns without paying for a discarded
request-wide heuristic scan. Network counters still use that prefilter at the
same threshold, and a later heuristic fallback reuses the already-computed
value. A usage-cache hit verifies the complete measured-prefix digest, exact model,
and system/tool envelope without copying the historical message list or
re-tokenizing the prefix. A sampled tail is not sufficient evidence: earlier
edits and cold history reconstruction invalidate reuse. On a miss, historical
provider usage must never cap a fresh count. Native post-compaction effective
usage cannot authorize the original pre-compaction message list.
The selected-slot body adapter adds a local-only hard-window check after route
adaptation. It recounts messages, system and tool schemas locally using bounded
content-digest tokenizer caches; unbound integers and same-model hints cannot
skip this safety check.
Over-window input raises the existing typed context-limit failure before
transport so the task can compact and retry; the adapter never silently
truncates user history. Opaque reasoning ciphertext length is not text-token
usage. Provider-native counts remain authoritative for billing.
Adaptive L2 compaction is opt-in. It compares route-priced, cache-adjusted
input savings with compaction-call and evidence-loss cost; context-window safety
remains an unconditional hard gate. Generated state is checked against pending
work and the evidence ledger; failed validation preserves the original context
for live/transient tasks. Only explicit utility callers may retain a deterministic
bounded view. `remainingRoundsMedian` is also the horizon for exact pre/post-summary
checks. Fixed compaction starts at one round, then uses the shortest of the four
most recent successful compaction gaps and the current window's observed age,
capped at six and by the remaining API-round budget. Long task age cannot hide
a three-round prefix lifetime.
An automatic economic decline records the optimistic token-growth lower bound
at which the candidate could first repay its cache rewrite or meet the minimum
reduction. Until that bound, later rounds reuse the preflight veto instead of
rebuilding the same fold candidate. A lower warm-cache witness or a longer
observed compaction window invalidates the veto immediately;
explicit/reactive compaction and the hard window gate always bypass it.
Pre-compaction archives remain exact audit snapshots. Metadata/summary loads
without the potentially multi-megabyte message payload; clients load messages
only for explicit inspection, copy, or download. The bounded
`tofu.compaction-receipt/v1` records strategy/fallback, preserved
anchors/turns/tool rounds/files, durable-objective application, summary time,
normalized usage, cache payback, evidence, and recovery truncation. Stored with
the archive, it never enters model context or enlarges the next request.


Model-projection display rules are owned by
[`model_context_v1.schema.json`](../contracts/model_context_v1.schema.json);
`scripts/gen_model_context_projection.py` generates Python/Rust exclusions.
Problem: handwritten rules missed the real translation writer's overlay fields.
Decision: exclude only declared projection and text/thinking segment roots;
real-producer CAS tests check preserved model revisions/continuations alongside
advanced display revisions and notifications on all storage authorities.
Alternatives rejected: recursive name stripping hides real tool evidence;
classifying every translation write as display-only hides changed `content`.
Unknown segment types, source edits and edit/revert remain protected. No schema
rewrite, revision rollback, worker or cache is added; old terminal witnesses may
rebuild once under the corrected projection.
Comparison smoke: 32 rounds with 8 MiB of shared immutable result text and one
segment overlay, 30 calls under tracemalloc, measured 0.330 ms median and 3,736
peak temporary bytes on Python 3.12.13. This measures comparison overhead only.
The viewer copies and downloads the same selected post-snapshot projection,
loading it once on demand and fencing late results after a selection change;
transport envelopes never become exported snapshot files.
