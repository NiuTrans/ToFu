# Context compaction policy
This guide owns the detailed compaction policy referenced by the
[context-engineering domain map](modules/context_engineering.md).
Automatic compaction bounds the request view without rewriting durable Turns. Working-state summaries retain known repository/root attribution for paths as rootname-relative or original absolute paths. A new task worktree is not evidence that every historical path moved. Unknown attribution stays unresolved; fresh environment mappings govern execution.
Manual compaction of an idle ordinary conversation writes a persistent summary boundary through the conversation command service, guarded against intervening Turns; a protected (coordinator or coordinated-project) conversation, and any conversation whose task is running, schedules a working-view compaction instead and keeps every canonical Turn. They share summarization and token-budget primitives, never persistence semantics.


### Manual admission ownership

**Problem:** a host repository lookup inside the reusable compaction engine can
check a different authority from the selected transcript store, breaking embedded
hosts and applying another store's policy to a destructive operation.

**Decision:** `ConversationStore.manual_compaction_rejection` reads owned policy.
The manual engine passes its captured store, explicit owner and loaded projection
before archive or model work. Only `None` admits preparation; a nonempty reason
refuses it, missing support returns `turn_protocol_unsupported`, and malformed
decisions or failed reads stop without rewriting history. The default adapter
uses `lib/conversations/compaction_policy.py` with its own client and rechecks
before `turn.compact`. A successful preflight never authorizes a later commit by
itself. Alternative stores implement this port and their own commit-time policy.

**Alternatives rejected:** importing application repositories into core,
silently falling back to host storage when the port is absent, or granting from
an earlier admission result. These break authority selection or lose intervening
protection. Executable specifications are the manual engine, core boundary and
coordinator lifecycle tests.

### Coordinator context lifecycle

**Problem:** coordinator (Router) conversations live for months, but manual
`turn.compact` deletes old canonical Turns and folds their projections. Dispatch
grants refer to the exact original human Turn and assistant Turn/Attempt, and
report attention refers to canonical report Turns, so the rewrite stayed refused
(409) and the owner of a long-lived Router could not compact on request at all.
Rebinding a new conversation or rewriting these rows is not a working-context
reset.

**Decision:** a protected conversation's compaction is a **scheduled
working-view compaction**; canonical Turns, Attempts, grants, reports, bindings
and budgets are never rewritten.

- Protection is the store's own policy read
  (`ConversationStore.manual_compaction_rejection`): owned coordinator creation
  identities, current bindings (including unavailable targets) and canonical
  report provenance (`coordinator_context_compaction_unsupported`), and every
  conversation still in an owned project with a nonzero binding epoch
  (`project_context_compaction_unsupported`; no partial history page proves
  absent source references). Current creation IDs stay protected across
  unbinding/project moves. Ownership failures and unknown storage/binding reads
  never authorize anything.
- `POST /api/v4/conversations/{id}/compact` serves those refusals by recording
  one durable owner-scoped marker
  (`ConversationStore.schedule_working_view_compaction`; generic record
  namespace `conversation.working_view_compaction.v1`, keyed by owner and
  conversation digest, value repeating both identities and verified on every
  read) and returns `{ok, scheduled: true, alreadyScheduled, requestId,
  requestedAt, protection}`. A repeated request while one is pending returns the
  same `requestId` (CAS create; a concurrent loser reads the winner). Scheduling
  touches no Turn, so it is accepted while a task runs. A store without the port
  refuses with `turn_protocol_unsupported`. Protection that begins during a
  destructive preparation still fails the commit-time recheck
  (`DefaultConversationStore.compact_turn_transcript`) and lands on the marker.
- The conversation's next model call applies it: the first
  `run_compaction_pipeline` of a root, checkpoint-publishing task (never a child
  invocation, transient runtime, Flow or Goal run) reads the marker once (again
  after a [wake hint](#busy-time-scheduling-and-cancellation)) and runs
  the existing forced manual L2 path (`trigger=manual`, no economic gate, the
  manual preservation budget). The carrier records `scheduledRequestId`, which a
  later summary folding it inherits; the existing L2 receipt and compaction
  archive mark the point in the transcript.
- Ordinary checkpoint publication adopts the summarized view. After terminal
  persistence `settle_scheduled_compaction` retires exactly that `requestId`
  (version-checked delete) only when the stored checkpoint
  (`_modelReplayPublishStatus == stored`) carries its carrier. A declined or
  failed summary, nothing foldable, a refused publication or a failed delete
  keeps the marker; the next publishing task retries, and a view that already
  carries the request only retries the delete.
- Ordinary never-coordinated conversations keep the destructive idle-only manual
  command. While their task runs, the same request schedules this marker
  (`protection: null`) instead; see
  [busy-time scheduling](#busy-time-scheduling-and-cancellation).
- The context meter's compaction action shows the scheduled state from the reply
  (`frontend/src/features/conversation-bar/scheduled-compaction.ts`) until the
  run that applies it settles; it reloads nothing, because nothing was
  rewritten. The acknowledgment is page memory: the server marker stays the
  authority, and a repeated request reports `alreadyScheduled`.

**Alternatives rejected:** keeping the 409 (a months-long Router cannot bound its
context on request); a same-conversation fresh/reset API (continuation
publication is monotonic at the settled Turn frontier, and a reset discards
working state); deleting then replacing a checkpoint, fabricating a summary Turn,
replacing the coordinator binding or adding a parallel transcript store (each
rewrites or duplicates evidence that grants and reports reference); summarizing
immediately in the route (the working view belongs to the next model call; an
out-of-band summary races the running task's publication and needs a second
adoption path); retiring the marker when the summary is generated (a refused
publication would silently drop the request).

A legacy arbitrary coordinator ID moved out of its original project without
report provenance has no complete reverse-reference index and is classified as
ordinary; this is an unresolved identification gap.

Executable specifications: `tests/tasks_pkg/test_coordinator_context_lifecycle.py`
(real SQLite and native authorities: scheduling leaves every canonical snapshot
unchanged, idempotent and owner-scoped markers),
`tests/compaction/test_scheduled_working_view_compaction.py` (pipeline
application, inheritance, publication and retirement) and the manual engine and
route tests.

### Busy-time scheduling and cancellation

**Problem:** long-lived Router conversations are busy most of the time, and
manual compaction refused every conversation with a running task (409
`task_active`), so the owner could not bound context on request. Rewriting
canonical Turns under a running Turn would race its events and publication.

**Decision:** while a task runs, the manual request schedules the working-view
compaction above for every conversation; the destructive rewrite stays
idle-only.

- `schedule_busy_compaction` records the same durable marker, and only after
  that commit `hint_scheduled_compaction` sets a volatile wake hint on this
  owner's live tasks of the conversation that can apply it
  (`applies_scheduled_compaction`: root, checkpoint-publishing, not transient,
  forced compaction not disabled). The reply adds `appliesAt`:
  `next_model_call` when such a task was woken (its next
  `run_compaction_pipeline` re-reads the marker and compacts that running
  Turn's request-local working view through the forced L2 path, which keeps
  tool-call/result pairs and anchors like automatic intra-turn compaction and
  touches no canonical Turn or event; if it makes no further model call the
  next reply applies it), otherwise `next_reply`. Protected conversations
  report `appliesAt` the same way.
- Retirement is unchanged: ordinary checkpoint publication adopts the view and
  `settle_scheduled_compaction` deletes exactly that `requestId`. A later idle
  persistent compaction also fulfils (deletes) a pending marker.
- `DELETE /api/v4/conversations/{id}/compact/scheduled/{requestId}` withdraws
  exactly that request (version-checked marker delete), then wakes live tasks
  to re-read it, and returns `{ok, cancelled, applying}`. A task claims a
  request atomically with the hints (`claim_scheduled_compaction` refuses while
  a hint is pending, so the pipeline re-reads first): a cancel committed before
  the claim is never applied. `applying` reports that a running reply had
  already claimed it; that working view stays compacted and no later model
  call repeats it.
- The hint only accelerates. The marker stays the authority, a lost hint
  degrades to the next reply, and each task attempts a `requestId` at most
  once.
- The context meter stays usable while a task runs: without a request its
  action reads "Compact at the next model call"; with one, a status line names
  when it applies and the action becomes "Cancel scheduled compaction"
  (`frontend/src/features/conversation-bar/context-meter.ts`).

**Alternatives rejected:** keeping the 409 (the owner of a busy Router can
never compact); stopping or waiting for the running reply (loses or delays work
the owner did not ask to stop); rewriting canonical Turns under a running Turn
(races its events and publication and could fold the Turn being written);
summarizing in the route (needs a second adoption path, as above); re-reading
the marker before every model call (one storage read per round for a rare
request, where the post-commit hint costs nothing without one).

Executable specifications: `tests/compaction/test_scheduled_working_view_compaction.py`
(wake, claim and cancel ordering), `tests/tasks_pkg/test_manual_compaction_route.py`,
`tests/tasks_pkg/test_manual_compaction_engine.py` and the context-meter and
scheduled-compaction Vitest specifications.

### Summary coverage and recall

**Problem:** an L2 carrier did not say which history it replaced or how to read
it again, so after months of successive compactions a Router could not tell
which Turns a summary stands for or recall their exact text.

**Decision:** the visible carrier names its covered Turn range before the
summary: `Covered history: Turns <first> through <last>[ and its reply]. Their
exact text stays in the durable transcript: get_conversation(conversation_id="<this
conversation>", before=<N>) ends at the last covered Turn; a smaller before pages
to older Turns.` The same facts are private metadata
(`_compactionHandoff.coverage`, contract `compaction_handoff_v1`). The first Turn
is inherited from a folded prior carrier; live rows without `_turnId` belong to
the reply Turn of the nearest preceding identified input (`throughReply`). `N`
is get_conversation's 1-based exclusive cursor: the last covered main-lane
ordinal plus 2, plus 1 for its reply. It is exact while ordinals equal positions
and otherwise an upper bound that never hides covered history; an unreadable
ordinal omits only the cursor. Child invocations, transient runtimes and
unidentified rows carry no coverage. The line precedes the summary, so
prior-receipt carry-forward (which starts at the first summary heading) never
copies a stale range. Reading the current conversation is the recall tool's
contract (`lib/conv_ref/`).

**Alternatives rejected:** metadata only (providers never see private fields);
copying covered text into the carrier (defeats compaction); timestamps as the
cursor (get_conversation pages by message number). Specs:
`tests/compaction/test_summary_coverage.py`.

### Auxiliary summaries under dispatch grants

**Problem:** a project Worker (and any consult or files grant) that reached its
window raised `Project consultation cannot purchase auxiliary compaction
summaries` and aborted, although the [no-spending-ceiling
decision](PROJECT_COORDINATION.md#no-spending-ceiling-2026-10-decision) gives
Workers the same ceiling-free grants as Coordinator continuations.

**Decision:** automatic L2 remains a request-view operation, and every dispatch
profile summarizes under **its own grant's existing physical ExecutionBudget**
through the ordinary `model_budget_admit` transport port, exactly like a
continuation. `summary_model_admission` revalidates the bound grant
(`validate_task(require_account=True)`: grant, birth execution, current Attempt
and account identity) before preparation and again at physical admission, and
refuses a swapped carrier. The account's observers record each occurrence on that
grant, so finite legacy or consult grants keep their recorded ceilings and
unknown-usage fences. Missing, swapped or withdrawn accounts, revoked grants and
unknown authority stop the task with the typed dispatch cause. No new source
account, tokens, model requests or deadline are issued; original human-task
summaries reuse their already attached account, and summary receipts/cost
telemetry remain separate from authorization accounting. **No profile stays
prohibited:** consult grants expose no tools, but a summary reads only the task's
own working messages through the same accounted port, so it adds neither data
nor tool authority, and their finite ceilings already bound it.

**Alternatives rejected:** keeping the Worker/consult prohibition (a long Worker
aborts at its window); a separate summary allowance (a second ledger beside the
grant); charging the delegating Coordinator's request (a Worker would spend
another grant's account).

Executable specifications: `tests/tasks_pkg/test_coordinator_context_lifecycle.py`
(real isolated semantic storage, exact source/Attempt/report/grant/budget snapshots)
and `tests/tasks_pkg/test_coordinator_summary_accounting.py` plus
`tests/tasks_pkg/test_consult_summary_accounting.py` (real grant/account observers
with simulated provider transport, exhaustion and unknown usage).

### Continuation checkpoints

**Steered turns — Problem:** routers receive frequent steers (human mid-turn
messages, timer and channel deliveries all enter with `inject_mode='steer'`), and
publication refused every Turn carrying `_userSteerInjects`, so a busy
conversation rarely checkpointed and each next request rebuilt every Turn. That
rebuild cannot even restore what the model consumed: the Turn projection keeps
only a 1,200-character display preview of each steer, never its carrier.
**Decision:** a steered Turn publishes its verified model view, consumed steer
carriers included. The round drain marks the task (`_inboxDeliveryUnverified`)
whenever it appends an inbox carrier, and only the verified provider-return flush
(`flush_deferred_peer_and_steer`, shared by the Python and native loops) clears
the mark. Publication refuses `unverified_inbox_delivery` while it is set,
because such a carrier's payload is salvaged to the durable queue (steer) or
redelivered from its durable row (peer, background, swarm). Steers that missed
the final step keep the existing queue path and enter the next Turn exactly once.
**Rejected:** keeping the refusal (unbounded rebuilds, consumed steers lost on
every rebuild); stripping unconsumed carriers from the published view (the final
answer was generated with them in view, so the stored view would be one the model
never saw); rebuilding steers from the Turn sidecar (lossy preview, a second
transcript authority). **Multi-Turn runs keep their refusal** (`multi_turn_run`):
Flow and Goal phases settle as later visible Turns while settlement witnesses only
the root Turn, so a root-frontier checkpoint would replay every phase again as the
uncovered suffix. Routers never run visible multi-Turn flows: dispatch grants
force `autopilot`/`activeFlow` off, and swarm auto-continue Turns already publish.
The compaction resume witness (`compaction_resume_witness`) applies the same
consumed/unverified rule. Specs: `tests/tasks_pkg/test_steered_checkpoint_publication.py`,
`tests/compaction/test_continue_working_context.py`.

**Policy drift — Problem:** any witnessed policy drift discarded the checkpoint and
rebuilt every Turn, and even a route switch rebuilt whenever the checkpoint held
reasoning or native Responses/Anthropic items, which every reasoning model
produces. A months-long conversation lost its bounded context on each model or
effort change. **Decision:** the checkpoint's field map names the drifted fields
(`policy_witness_drift`). Drift confined to route and effort fields
(`PORTABLE_DRIFT_FIELDS`: model, provider, preset, `modelRef`, preferred
provider, routing, `thinkingDepth`, `thinkingEnabled`, `defaultThinkingDepth`)
restores the portable projection (`restored_portable`): provider continuation
state (reasoning text and signatures, `reasoning_details`, opaque Responses
items, native Anthropic blocks) is stripped, Composer carriers are recomposed for
the destination, tool calls/results and Tofu carriers (steers, inbox and mailbox
receipts, handoffs) keep their exact bytes, and only the uncovered suffix is
rebuilt. Every other witnessed field is authority-bearing and still refuses
(`policy_changed`): permissions, project scope, tool authority and capability
toggles, experiment flags, instructions, principal, goal mode and the interaction
mode. Plan and Delegate now witness `interactionMode`; Standard keeps the absent
value, so existing digests are unchanged. Media and unknown private state still
refuse (`nonportable_checkpoint`), legacy checkpoints without a field map keep
the route-free digest comparison, and the tool latch and goal state remain
same-policy only. **Rejected:** refusing on reasoning (no reasoning-model
checkpoint survives a route change, and a cold rebuild never replays foreign
reasoning either); an allowlist of authority fields (a new witnessed field would
silently become portable; default deny instead); keeping full signed views on
every effort change (signatures and caches are route-bound; only the same-model
Opus effort witness keeps them). Specs:
`tests/model_routing/test_model_continuation_policy.py`,
`tests/project_mod/test_coordinator_replay_policy.py`.

**Settled reasoning on restore — Problem:** a checkpoint keeps its publishing
Turn's rounds exactly as sent live, without the settled-Turn mark that a cold
rebuild derives from `_turnStatus`. The next Turn restored that signed Claude
reasoning as active, so when the dispatcher picked Chat Completions or Responses
after restoration, request preparation refused with
`mandatory_continuity_unrepresentable` before dispatch. A failed Turn never
publishes, so every later Turn restored the same checkpoint and failed too.
**Decision:** `restore_prefix` marks each restored assistant row that carries
signed reasoning as a completed Turn (`_reasoning_continuity: completed_turn`),
the same authority as the cold rebuild: restore covers only settled Turns, since
resume data and resume targets refuse first. Another protocol then omits that
historical thinking as a counted loss, while Anthropic replays every block
unchanged. The stored checkpoint is not modified. **Rejected:** stamping the mark
at publication (existing checkpoints would stay broken, and a failing
conversation never publishes again); treating unmarked thinking as optional in
the converter (live signed reasoning of an active Turn would be dropped); a
portable restore whenever the protocol might change (the destination is unknown
at restore time, and same-protocol replay would lose its signatures and cache).
Spec: `tests/conversation/test_completed_turn_reasoning.py`.

L1 receives an API-form working projection and replaces only cold tool/image bulk; it performs no conversation read, projection update or change notification.
Completed ordinary chat turns publish this bounded view through the owner-scoped durable continuation repository. The next turn validates it and transforms only uncovered Turns; restarts/cache eviction do not resurrect summarized history. Missing/invalid checkpoints rebuild unchanged Turns and pass fresh admission.
**Continuation boundary:** display updates previously invalidated summaries, while shared eligibility wrongly blocked completed Continue publication. Decision: semantic proof v2 preserves typed content/tool/Attempt/policy/media fences; Continue proves its input baseline, retains fresh suffixes, and publishes only verified complete model content. Rejected: weaker witnesses, history deletion or higher thresholds. Old proofs rebuild once with explicit reasons. Details: `contracts/model_context_v1.schema.json` and [storage operations](STORAGE_OPERATIONS.md#model-context-semantic-version).
**History length:** a lane keeps publishing and restoring checkpoints for any number of Turns. Both authorities stream the v2 proof digest over every header and materialize only the uncovered suffix; proof counts are plain counters and only legacy per-Turn witness lists keep a count bound. Rejected: raising the former 10,000-Turn ceiling. Details: [long-horizon context reads](TOFUDB_CONTEXT_READ.md#streamed-proof-walks-without-a-turn-ceiling).
Ordinary `excludeLast` excludes the empty output target, not continuation itself: validate that boundary. Actual resume payloads and nonempty resume targets cannot blindly restore a completed prefix; fresh execution boundaries and changed historical witnesses also refuse restoration.

**Ingress-policy continuity — Problem:** Continue/resume could exit prefix admission before pinning its policy, then publish a checkpoint hashed from runtime-normalized configuration. An unchanged next request rejected that checkpoint and rebuilt already-folded history. **Decision:** pin strict, portable and supported effort witnesses with the input history baseline before resume exclusions; successful, verified completion publishes those original witnesses. Real policy/project/permission changes still reject reuse. A validated-prefix refusal that requires a full-history reread preserves and logs its original reason (for example `policy_changed`), not the internal `replay_disabled` fallback status. **Rejected:** increasing compaction thresholds, equating absent policy with false, or bypassing owner/history/media fences. Specs: `tests/model_routing/test_model_continuation_policy.py`, `test_model_context_read.py`.
L1 indexes diagnostic/pairing/newest-unconsumed positions once per pass; input-bounded references expire on return. Reverse selection, occurrence IDs and consumption stay intact. Previews inspect at most 121 characters for two-line/120-character output.
`tool_compacted` stays durable-before-visible without tool-row mutations; DEBUG success logs and content-free aggregates report emitted/failed counts and monotonic durations.
Pipeline timings distinguish L1, L2 (checks/provider/persistence), recomposition (including analytics) and advanced work. `done` means normal return, not adoption. L1 total includes indexing/counting beyond event time; timing creates no authority.
Summary formatting counts once; centre-first `O(n log n)` elision uses constant-time length deltas, preserves human text and reports irreducible overflow honestly.
### Shared child invocation ownership
`compaction/_ownership.py::compaction_owner_key` is the working-state identity boundary. Root/direct callers retain their existing conversation keys. A child identified by `is_child_invocation(task)` uses `invocation:{user_id}:{task_id}`; its parent `convId` remains an authorization link, never its compaction cache or accounting bucket. Explicit legacy `conv_id` arguments cannot override child identity. Missing child task identity fails closed.
L1/advanced prefix reads, L2 admission/cooldowns, reactive invalidation, summary affinity and usage, archive creation/retention and prior-receipt lookup share this key. Real compaction and canonical call-level usage/receipts remain enabled. Children do not re-pin the parent's durable objective from generated summaries.
No new ledger, store or worker is introduced. Child success and fatal/cancel handlers drain summary costs through `_settlement.py` before terminal delivery; the drain is idempotent and retains call-level costs. Provider token/cache writes and final cache cleanup use the same owner key; teardown releases only the owning child's cache and cooldown entries (see the lifecycle contract below).
Storage remains the existing `ConversationStore` authority. A task-scoped key is **not** a durable conversation ID: child invocation integration must supply an owner-authorized task archive scope through that repository boundary, not create a synthetic conversation or fall back to the parent's archives. The default conversation-only adapter alone is insufficient for child receipt durability.
`tests/tasks_pkg/test_child_compaction_ownership.py` specifies sibling/root isolation, canonical costs, cooldowns and create/read/prune scope parity with isolated fakes. Final recovery reuses that scope; metadata failure never falls back to parent archives. These specs cover usage settlement, not real Sidecar scope or provider cache cleanup.
### Assistant phase and task continuity
The default operating prompt interprets action requests as scoped execution, preserves authorization from the conversation, and carries the original task through status questions and compaction. It asks for essential missing input or new authorization after completing independent authorized preparation. Explanation-only requests, tool permissions and destructive-action boundaries remain explicit. The prompt is still composer-owned and cache-stable.
Single-agent Responses assistant messages retain each original `commentary` / `final_answer` phase and message boundary in the existing `_responses_items` sidecar, shared by live, tool-round and durable history reconstruction. Adjacent assistant rows carrying native state are not merged. Every Responses route can replay these messages; model-specific opaque-item feature gates are unchanged.
Replay requires matching current canonical prose, so redaction, hooks and compaction cannot resurrect stale captured text. User messages never gain a phase. Native multi-agent messages retain their separate attribution policy.
This reconstructible sidecar adds no store or worker. Capture is bounded to 64 messages, 64 content parts per message and 256 KiB of UTF-8 text per response; overflow or unsupported message shapes omit phase replay while retaining the ordinary transcript text. Existing continuation/Turn storage bounds and lifecycles still apply. Regression and budget evidence lives in `tests/llm/test_responses_message_phase.py`; terminal handling is specified in [TURN_SETTLEMENT.md](contracts/TURN_SETTLEMENT.md).
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

**Problem:** repeated composition reparses unchanged gateway receipts, while
message/list identity cannot prove that a receipt survived clipping or call
rebinding. **Decision:** each projection pairs the actual assistant/tool history
and consumes each declared occurrence once. Only receipt interpretation is
memoized, by exact immutable text chunks and direct/gateway kind, within the
current round, message list and normalized accepted checklist values. Failed
scans publish nothing; compaction candidates bypass the memo. Each successful
scan replaces its cache with only still-paired receipts, so retained entries
scale with admitted history and release with the task. Text chunks are borrowed,
not copied into another joined payload. No cache entry authorizes a tool.
**Alternatives rejected:** identity-only prefixes miss in-place edits; frozen
whole-history prefixes add overhead to ordinary reads. Pairing remains O(messages).

Measurement: Python 3.12, synthetic 128/2,048/8,192-message histories with 4 KiB
read receipts and identical public checklist projections. Warm gateway histories
improved by 8.9–9.8×; the 8,192-message case went from 31.20 to 3.50 ms. Direct
reads improved from 2.06 to 1.77 ms at that size. Traced retained allocations were
about 403 KiB for the gateway case versus 322 KiB previously, and 272 bytes for
direct reads versus 322 KiB. These are local component measurements, not live
request latency. Specs: `tests/goals/test_checklist_evidence_memo.py` cover reuse,
in-place changes, partial batches, duplicate IDs, failure atomicity and candidate
isolation alongside the restoration suite.

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

**Joint L1/L2 projection.** Problem: ordinary L1 protects already-sent cache
bytes, but L2 planned its replacement from that same bulky projection. Decision:
L2 runs the existing deterministic L1 transforms on a detached candidate with
the cache fence lifted, then selects whole semantic source units from it. The
newest unread tool batch, active skills, user intent and provider-required
reasoning keep their existing protections. Unchanged messages retain their
identity. The summary receives the original observations for the selected
units, so preprocessing cannot replace error or mutation evidence with a short
preview. The archive and coverage likewise describe the original request.
Preparation emits no L1 events, writes no artifacts and changes no task state;
only the existing successful L2 adoption publishes the combined result. Empty,
failed, cancelled, over-budget and uneconomic candidates preserve the original.
Explicit L1 ablations remain respected. There is no additional model call,
trigger, hot-tail knob or retry budget.

Scoped project guidance is retained according to the same remaining tool targets
used by its restoration owner. Root rules and guidance for retained work remain
verbatim; verified inactive directory sections leave the candidate, including
mixed carriers. Unknown provenance stays intact. Restoration and economic
measurement use that same scope projection, so inactive rules are neither
re-injected nor charged as an imaginary restore reserve. The next access
reacquires them through ordinary project guidance. Alternatives rejected:
eagerly mutating the live prefix before summary success; summarizing L1 previews
instead of original execution evidence; retaining every historically acquired
rule forever; and changing cache thresholds to compensate for these boundaries.
Specifications: `tests/compaction/test_compaction_joint_projection.py` and the
project-guidance delivery/retirement tests.

Frozen-request measurement (47 messages, original 109,921 local estimated
tokens; archived summary reused, acquired guidance scopes reconstructed from
the archive): the original accepted projection is reproduced at 74,520 tokens.
Scope retirement reduces it to 29,372; scoped-rule material falls from 50,987
to 5,839. L1 alone leaves this particular text-heavy fixture unchanged, and its
formatted summary history stays at 6,603 locally counted tokens. Local
projection medians were about 10.5 ms before and 16 ms after; provider latency,
storage and final request recomposition are excluded. These are message-view
measurements, not the UI's calibrated full-request count or a measured reduction
of the 42-second provider call. In the synthetic 18-image-round regression,
joint planning reduces selected summary source messages from 28 to 20, formatted
history tokens from 1,078 to 770, and the retained projection estimate from 3,479
to 2,105 while preserving the newest unread batch and original summary evidence.

**Local L2 ownership.** Problem: one entrypoint mixed source selection, economic
eligibility, summary fidelity and final window checks with durable adoption.
Decision: `_layer2/_source_plan.py` returns whole source units and output reserve;
`_candidate_policy.py` distinguishes preflight cost from paid-summary accounting
and future adoption cost; `_summary_acceptance.py` validates critical state and
fits optional recovery hints; `_projection.py` shares retained-shell construction
and candidate convergence. `_compact.py` alone orders generation, cancellation,
archive creation, accepted receipt persistence and model-view adoption. Named
ports bind its existing injected policies; phase records are per-call values,
not another durable state machine. Phase diagnostics keep the established
`_layer2._compact` logger so existing listeners still receive every decision.
Alternatives rejected: moving the full function
unchanged, hidden imports back into the entrypoint, or independent persistence
owners in each phase. Frozen-source comparisons require identical messages,
metadata, owner state, usage and ordered side effects before component clocks.

Economic eligibility estimates run only for proactive compaction. Manual and
hard-window compaction still enforce source preservation, fidelity, capacity and
durable adoption, but do not scan cold history or restored snapshots for unused
return-on-cost estimates. Paid summary usage remains accounted on every path.
Keeping those scans on mandatory paths adds history-sized work without changing
their admission decision; sharing the candidate policy keeps the economic path
and its conservative restoration reserve intact.

**Unverified-pricing deferral.** Problem: most proactive declines lack verified
main-route pricing, yet each first built the journal-backed turn diff and
selected a summary route, about 70% of a deferred attempt's wall time in a
production profile. Decision: without verified
main-route rates for the current prompt (`main_route_pricing_verified`, the
lookup behind `pricing_known`), payback is infinite for every candidate and
route. One per-attempt verdict therefore skips both steps and declines through
the unchanged `cache_negative` branch: same message, retry floor and witness.
The decline line reports `route_reason=main_route_pricing_unverified`; the plan
reserves no diff text for a continuation never built, so a diff that alone
would exhaust the plan now defers instead of refusing. Priced input keeps the
full path, including an unpriced resource summary route; forced, hard-window
and dispatch-guard attempts never consult the probe. Alternatives rejected:
per-task route memoization (input size and credential health vary by round;
the cost is routing-document normalization owned by `lib/model_routing`), a
candidate-free retry floor for unpriced declines (changes later attempt
timing) and a bounded turn diff kept in the plan (its first section is
unbounded and owned by `commit_round`). Spec:
`tests/compaction/test_l2_unverified_pricing_deferral.py`.

**Summary request ownership.** Problem: evidence formatting, routing authority,
credential cleanup, transport completion and usage accounting shared one large
closure. Decision: `_summary_input.py` assembles owner-scoped receipts and bounded
evidence; `_summary_transport.py` keeps provider-work observations and call usage
for one logical summary. `_summary.py` retains route/quote revalidation, the
credential lifetime and final budget settlement. It passes the current authorized
route into each transport attempt; credential requalification still requires a
proven unsent failure. Typed stream verification, paid incomplete usage,
parent affinity/pin restoration and the original diagnostic stream remain shared.
Alternatives rejected: module-global transport state can mix concurrent owners;
separate streaming and chat retry policies duplicate the dispatch authority;
moving credential cleanup into transport hides failures from budget settlement.
The summary-routing, credential-failure and route-cleanup tests specify these
boundaries.

**Problem:** a long archive exceeded native ordinary literal limits; adopting a summary after failed archival left Continue rebuilding cold history and hid the compaction marker. Startup compaction also lacked a zero-tool-prefix witness. **Decision:** native archive bodies use their declared 64 MiB blob budget and a compact, digest-bound outbox; ordinary literals keep the 8 MiB budget. For a Turn-bound task, the accepted post view and receipt commit before model-view adoption. Create/update failure retains the original input, emits an explicit failed compaction fact without an archive link, and stops provider dispatch. The pending compacting phase stays visible before an archive exists. An empty tool prefix is a valid startup witness. A closed suspended task saves its bounded working view through the same owner-scoped archive repository, including delivered background context and exact tool receipts. This checkpoint retains the original audit snapshot, makes no model call, emits no fictitious compaction, and follows the existing archive retention limit. Owner, Turn, history, policy, media and complete tool-prefix checks still fence restoration; changed or unproved input rebuilds rather than weakening those fences. Current logical-Turn loaded Skill batches remain verbatim through L1/L2; older Turns remain eligible for normal compaction. **Alternatives rejected:** raising all native literal limits broadens unrelated operations; adopting unsaved summaries breaks recovery; hiding pending or failed work makes repeated summarization invisible; relying on a tool-count watermark alone loses late background evidence. Specs: `test_continue_working_context.py`, the cross-backend large archive/restart producer test, and `test_visual_continue_compaction.py`.

Accepted L2 work emits `phase=compacting` before summary I/O; declined preflights stay silent and archive completion retains its durable boundary.
A summary generated inside a live task turn shares the turn's abort signal:
when the task dict carries the runtime `aborted` flag, generation streams
even without a UI delta callback so the transport watchdog interrupts it
mid-flight, an already-aborted task skips the paid call entirely, and an
abort never falls through to the text-tier retry. Synthetic task dicts
(manual /compact, standalone callers) keep the non-streaming dispatch path
unwired.
Default task-owned summaries use a separate request-owned owner/tenant route.
Known-price organization-billed paid APIs rank before the owner's
subscription/OAuth/local resources, which a paid main that fits the summary never
borrows; each class ranks cost first, then highest AA. A route without its own
rate card is priced at its model's maintained price, and there is no summary cost
ceiling; subscription marginal cost remains unknown. Preflight does not resolve
secrets. Dispatch binds the planned catalog revision/identities, mints only one
candidate with no widened fallback, nests/restores the parent provider scope and
cleans credential references after complete-output accounting. The parent Codex
pin does not override a selected paid provider. Complete output and durable
adoption checks remain unchanged; no route failure licenses source deletion.
Scope, evidence, opt-out and ranking semantics live in
[AA-cheapest compression-first routing](CONTEXT_BUDGET_POLICY.md#aa-cheapest-compression-first-routing).
### Complete summaries without a wall-clock deadline
**Problem.** Short timers can discard useful slow work or trigger lossy recovery;
clipped or provider-truncated summaries cannot safely replace source context.
**Decision.** Elapsed time is telemetry: legacy `summaryBudgetSeconds` values
(including zero) impose no deadline. Owner Stop, transport failures and bounded
429 retries remain effective, without a detached worker. Failed identical requests
latch against repeated cost; changed evidence/route changes the request digest.
Completed late output is accepted normally.
Owner Stop is rechecked after final post-summary measurement; it withdraws any
current credential-failure projection and follows the existing cancellation path,
never login-recovery diagnostics or another main dispatch. A stopped task cannot
reconstruct earlier credential authority from a still-held usage projection.
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
**1-hour cache writes (2026-10-23).** Problem: summary call costs kept
`pricingSnapshot.rates.cacheWrite1hMul` but dropped `cacheWrite1hTokens`, so a receipt
could neither show which writes paid the 1-hour rate nor recompute its amount.
Decision: priced calls and validated stored rows carry the count emitted by
`lib.cost.compute_cost`; a stored count must equal the engine's share of that call's
usage, and rows priced before 1-hour rates existed carry none and stay valid evidence.
Rejected: deriving the share from receipt usage (no TTL breakdown) or repricing stored
rows. Spec: `tests/compaction/test_summary_cost_economics.py`.
Logs expose identity, reasoning length and first-text latency, never reasoning content.
**Rejected.** A 60/120-second deadline, clipping, pricing-as-capacity or destructive
live recovery improves apparent latency at the expense of state. Executable specs:
`tests/tasks_pkg/test_live_summary_budget.py`, `tests/tasks_pkg/test_transient_summary_budget.py`,
`tests/compaction/test_summary_completion_policy.py`.
### Credential-outage robustness and authentic failed-attempt settlement
**Problem.** When the owning subscription's shared credential cannot be read
(`LocalRequestPreparationError` wrapping `SubscriptionCredentialUnavailableError`,
e.g. `codex_command_unavailable`), the L2 summary previously abandoned its first attempt, so a
long conversation over the working-set threshold deadlocks: every round-start
`working_set` compaction archives + emits `COMPACTION`, fails the summary, and
returns False, then the dispatch-guard admission pass repeats the summary and
raises a fatal `ContextCompactionError`. The failed attempts also left no
terminal fact, so the UI showed two stuck `running` "compressing" ghosts with
meaningless durations (the muu1klptjtwd3t incident).
**Decision.** Four scoped changes, none of which weaken the lossy-recovery
invariants for live/transient tasks:
1. Summaries reuse the main request's bounded credential-recovery owner for
   proven transient failures before provider ingress: at most three attempts
   within 30 seconds, preserving the exact payload, model, provider pin and
   approved quote. Cancellation interrupts backoff; permanent credential faults,
   local payload defects and observed provider work never retry. An alternate
   model name is insufficient authority to move a pinned or quoted summary.
   An unquoted request-owned auxiliary summary may then independently recompile
   another credential source, with at most three qualified routes in the same
   SummaryBudget. An unsent sign-in requirement (`SubscriptionLoginRequiredError`,
   e.g. a Codex login whose refresh was rejected) skips the same-login retry and
   requalifies the same way, but is never availability evidence. Full owner/tenant/revision, parent identity, policy, capacity
   and paid-quote checks remain in force; the previous group closes first.
   All transport attempts must be free of observed ingress/work, not merely
   the final local exception. Exact-source exclusions stay local to this
   auxiliary request. If nothing independent qualifies, one bounded attempt
   on the task's own authorized main route follows, unless every slot of that
   main shares the failed Codex resolver. Details and expiry are in
   [compression-first routing](CONTEXT_BUDGET_POLICY.md#aa-cheapest-compression-first-routing).
2. Explicit legacy utility callers may opt into deterministic `_head_truncate`
   when a summary cannot run and the conversation exceeds the usable window.
   The dispatch-guard trigger grants no additional permission. Live/transient
   bounded tasks keep their no-lossy-recovery discipline; the proactive
   `working_set` pass keeps its summary-only economic path.
3. A failed `working_set`/`window`/`dispatch_guard` attempt settles as one
   terminal `compaction_done` whose receipt carries `status='failed'` and the real
   `outcomeReason`, bridging the live phase row through the existing reactive
   archive seam so the frontend renders an honest failed compaction row instead
   of a ghost. Reactive/backfill/heal and deterministic-recovery paths keep their
   own terminal accounting and are untouched.
4. The canonical receipt adds bounded `summary.failure` and `summary.attempts`
   fields: at most three routes, allowlisted source/availability reasons and
   known ingress facts, never raw errors, credentials, source references or
   request digests. Final admission attaches the existing typed subscription
   cause only from the current issued in-process SummaryBudget projection.
   Copies/JSON can retain diagnostic data but cannot acquire that authority.
   Cancelled, malformed, stale or prior-ingress evidence cannot project an
   overall unsent login outage; later cancellation, fatal exits and teardown
   defects withdraw earlier credential causes. Generic pipeline faults remain
   internal. When all qualified sources are unavailable, live/transient
   history stays unchanged, and an overfull main request is never built/sent.
   Physical headroom may still defer optional compaction under the same safety
   and output-reservation checks. Metadata is not authorization to rebind an
   account or delete source history.
`llm_fallback/_call.py` applies the same discrimination: a subscription-credential
outage rotates like any primary-model failure (and permits pool rescue), while a
payload defect still refuses rotation.
**Rejected.** A deterministic *summary* last resort for live tasks (violates the
"source must survive a failed summary" invariant that `bounded_summary_task`
encodes); blanket rotation of every `LocalRequestPreparationError` (replays local
payload bugs across the pool); settling every failed attempt (double-counts the
reactive path's own receipts). Executable specs:
`tests/compaction/test_compaction_failed_settlement.py`,
`tests/compaction/test_summary_credential_rotation.py`,
`tests/compaction/test_summary_credential_resilience.py` (real compiler, mint,
transport observer, compactor and ROOT refusal boundaries), and
`tests/compaction/test_summary_failure_projection.py`.

**Utility settlement ownership — Problem:** successful and no-progress utility
recovery duplicated archive updates and completion events, making usage, scope
and outcome fields prone to drift. **Decision:** `_layer2/_compact.py` keeps
admission, pre-mutation archival, cancellation and mutation in the coordinator;
one local settlement helper publishes the chosen outcome. Only successful
recovery supplies a detached post snapshot; no progress remains a failed
receipt. Missing archive identity emits no completion, and receipt-write and
event-publication failures retain their independent best-effort behavior.
**Alternative rejected:** routing cancellation or model-summary adoption through
that utility helper would conflate their distinct persistence and failure
contracts. Executable specs: `test_compaction_fallback_dispatch_attribution.py`
and `test_compaction_handoff.py` under `tests/compaction/`.

**Auxiliary teardown ownership.** `ExitStack` consumes a failed callback, so
one failed physical detach could otherwise leave a credential-bearing slot
callable after its request ended. The summary owner fences only its issued
slot objects before release and joins one retry of the existing idempotent
disposer when the group is still incomplete. The first cleanup failure remains
visible, prohibits adoption/requalification, and withdraws credential-outage
authority; owner Stop still settles as cancellation. Persistent failure stays
explicit and uncallable, not an unbounded retry. Rejected alternatives: replaying
model work, changing global credential health, or swallowing release failures.
This control assumes physical release can fail before disposal commits; remove
it when the shared disposer guarantees atomic release or owns equivalent bounded
joining. `tests/compaction/test_summary_route_cleanup_owner.py` covers transient
and persistent detach failure, success, outage, fatal exit and Stop.

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
and a 48K estimated-token ceiling. Its ordinary pass never edits what the previous
request sent, except that request's last tool step when the exact next-request price
admits it ([sent-prefix admission](modules/context_engineering.md#l1-sent-prefix-admission)),
and always protects the newest complete tool-call batch, even when that batch alone
exceeds 48K. Older reconstructible results become request-local placeholders;
their durable tool results and artifact references remain untouched. The opt-in
`adaptive_hot_tail` experiment is not a second default authority.
Assistant tool-call turns omit empty `content` (`''`, JSON `null`, `[]`,
whitespace-only) at `_strip_empty_text_blocks` and again in cache Phase 0.5.
Present vs absent `content` is canonical-invisible but byte-divergent, so it
rewrites the GLM/Anthropic prefix cache every round. Specs:
`tests/llm_sanitize/test_empty_text_block_wire.py`,
`tests/cache/test_cache_content_wrap_invariance.py`.

Structured plain text separators are not phantom empty content: the shared
`llm_sanitize.text_blocks` authority folds exact spaces/newlines/tabs into a
compatible neighbor before strict-provider emission, without moving bytes
across cache/signature/opaque/multimodal boundaries. All-blank content still
heals/omits as above; ordinary single-string reports remain unchanged. Specs:
`tests/llm_sanitize/test_structured_text_whitespace.py`. Alternatives rejected:
keeping standalone blanks restores HTTP 400s; guessing spaces in saved messages
rewrites evidence and identifiers. `swarm_messages_v1.json` requires readable
authored prose, while message storage/injection preserve exact submitted text.
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
`compaction/_history.py` owns human-source classification and historical user carriers. A provider's `role=user` alone does not prove a human request: runtime meta blocks, virtual-user directives and ordinary inbox notifications are not human steering; explicit operator/human steering remains eligible. The opening request survives as **historical evidence**, not as the default current ask. Earlier selected requests from the folded region use one bounded policy across repeated compactions: newest-first under 4,096 estimated tokens and eight messages, then chronological rendering, with the opener separately protected. Historical users in protected execution shells have a separate preservation allowance; the receipt's retained-user count includes both regions. Full request accounting includes every carrier and its wrapper. Selected strings retain full text and boundary whitespace, including distinct equal-text occurrences; multimodal content blocks remain intact. Unselected originals remain in the transcript/pre-compaction archive. Request-local carriers use producer-owned `_retainedUserHistory` metadata for reselection and visible `<retained_user_messages>` attribution for providers that strip private fields. The version remains private metadata, not prompt prose. Legacy trusted carriers remain readable; pasted user XML never proves producer identity. Adjacent trusted carriers coalesce into one historical region without crossing human/execution boundaries or deduplicating equal text. One full explanation appears in the first carrier of the rebuilt projection; other carriers retain balanced tags and per-entry historical labels, not another copy of the long guidance. Surviving assistant/tool rows remain in causal order. Emergency image stripping also updates request-local typed history; rebuilding cannot restore stripped payloads (`tests/compaction/test_compaction_history_image_recovery.py`). The explanation marks history as non-live, later human steering as controlling, and existing scoped authorization as preserved without new approvals. Automatic L2 and manual `/compact` select one chronological complete tool-round suffix **across user turns**, not a fresh allowance per assistant. The count is a maximum under the preservation-token budget; enclosing user/assistant shells are charged too. The newest complete real execution batch survives even when it exceeds that budget. Synthetic receipts cannot displace real execution; incomplete rounds are not relabelled complete. Emergency whole-message removal protects the latest human and real batch; oversized irreducible carriers cause admission refusal. Single-result byte recovery may shorten reconstructible execution text with an explicit elision marker, never the required handoff. The receipt is fallible working state: objective, constraints, verified work, current state, blockers and next steps. Its **Objective** is model-authored from verbatim human evidence; summary-input elision removes non-human prose, not human text. A missing/blank Objective stays unresolved, never falls back to the opening request. Continue/no-ask instructions retain their scope, later stops or replacements win, and assistant proposals cannot become approval gates. The advisory autopilot pin is updated only from an accepted receipt. Manual compaction freezes its read set before summary generation, reconciles unchanged folded prefixes with concurrent tails, and suppresses repinning if the accepted human read set changed. This is not a new authorization store. **Ruled Out / Dead Ends** retains verified negative conclusions with proof and retry boundaries; pending steps cannot re-propose them without superseding evidence. If all routes are ruled out, report the blocker. The newest typed handoff, complete adjacent legacy `context_compact` pair, or manual summary is carried forward under a bounded budget; chronology, not format, chooses it. Automatic L2 also supplies the latest completed assistant wrap-up from the full source. `_layer2/_summary_evidence.py` supplies adjacent occurrence-local tool observations: newest 256 messages, at most 64 calls/message, 16 rows, 4 KiB/row and 24 KiB total. Parsing caps at 32,768 characters; non-text/encoded bodies are omitted, previews use bounded head/tail samples, and oversized identities are omitted whole. All wrappers/observations enter the existing summary-input budget; human text is never silently elided. Calls express intent, missing/partial results cannot prove completion, and omitted observations are not verified negative evidence. The prior receipt occupies one slot; default summaries never extract a duplicate ledger. Opt-in `evidenceLedger`/adaptive validation retain the separate runtime-ledger contract, not this default extraction bound. All are fallible evidence; newer execution wins. Specs: `tests/compaction/test_compaction_summary_evidence.py`. No new store, worker or I/O. Without a recognized live receipt, load only the newest owner-scoped archive summary metadata, never the archived transcript. Text-only route switches retain compacted state and its private provenance if non-route policy matches; v2 `modelRef`, preferred provider and routing are route identity. Manual compaction keeps selected human Turns/identities, never fabricated users. Reload groups consecutive users before the summary into one historical carrier; recent exchanges remain ordinary dialogue. CAS/archives use raw Turn space, never indices from expanded model messages. The receipt targets 800–1,600 tokens with an advisory 2,200-token size budget; reconstructible `data/tool-results` paths never become durable working files. Tofu adopts summary-last placement, not Codex `compact.rs`'s full rewrite: execution tail, typed provenance, immutable Turns and append-only context survive.
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
Proactive cache economics charge the summary call before dispatch at the quote
of the exact request dispatch sends
([quote contract](CONTEXT_BUDGET_POLICY.md#aa-cheapest-compression-first-routing)).
Current and candidate prompt prices are resolved independently, so a
provider-declared context-tier change affects the calculation without a model
special case. Recurring savings compare the observed warm prefix before and
after the rewrite; uncached tail tokens are not assumed to become cache reads.
The warm prefix is the last same-task, same-model/provider round's cache read
plus its cache write, so a cold full write (read 0) is fully warm for the next
round. After two consecutive failed readbacks (read below half of the prior warm
prefix) only the read counts: a route that stopped reading back does not shield
its prefix (`_cache_evidence.current_warm_prefix_tokens`). On such a cold route
every token outside the read prefix is billed again each round, so dropping it
counts as recurring savings at the plain input price; while a route still reads
back, dropped uncached tokens claim nothing.
Once a summary has been generated, that cost is sunk and adoption compares only
the future prefix rewrite with future input savings. Archive and UI token
counters are explicitly heuristic (`tokenCountKind=estimated`), not
provider-billed usage.
Archive and receipt `tokensBefore`/`tokensAfter` count the complete message
lists at the saved compaction boundary: policies, history carriers, preserved
execution, complete handoff and live input. They are not summary-token counts.
The Turn row (`.compaction-token-stage.is-after` on the card) starts from them
and then settles on `compaction_request_size`: the gate counter over the complete
request before compaction, and over the rebuilt request after
`recompose_context_after_compaction`, restored managed context and tool schemas
included (`docs/contracts/EVENTS.md`, activity timeline projection). The
receipt's own list omitted both, so conversation `mw10w4j3ghfsbk` showed 82%
where the next request was billed 49% smaller. Specs:
`tests/compaction/test_compaction_request_size.py`.
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

The compaction receipt is one quiet full-width activity row, not a tinted
callout: header (status, bare summarizer name, duration, always-visible
"View snapshot" action), then the token flow plus one plain-language cause.
Every recorded trigger has a label in `activity.compaction.trigger.*`:
`working_set` (economic threshold, "Long conversation" / "对话内容较多"),
`window` (model-window safety, "Context limit reached" / "接近上下文上限"),
`dispatch_guard` (pre-send admission, "Message too large to send" / "发送内容过大"),
`context_overflow` / `reactive` (provider-reported overflow),
`manual`, `force` and `checkpoint` (saved on interruption). The first three
describe local preventive admission, not a provider-reported overflow.
Internal budget names, raw threshold strings, the trigger id and routing ids
(main model, summary provider, selected model) live only inside the disclosure;
an unrecorded summarizer renders no invented name, only an honest tooltip.
Summarization uses a separately prepared, bounded request: the
main context's threshold does not imply that the summarizer received that entire
context or reached its own route limit. UI specs: `activity-block.test.ts` and
`tests/visual/test_visual_compaction_trigger_wording.py`.
A compaction that a resumed attempt did not reuse keeps its row, duration,
token flow and snapshot, but reads `superseded` ("Context compaction discarded
on resume" / "上下文已收束 · 续跑时作废") with muted, struck-through counts and
its cause in the visible detail; the resume report that decides this is
described in [contracts/EVENTS.md](contracts/EVENTS.md).
The local token authority reuses exact-ish counts above 4 KiB, or above 512 characters when a caller proves reuse. Full repeated requests batch all short text plus unique cold misses once, while stable large-prefix digest hits skip BPE work. Encoding/length/SHA-256 keys retain no prompt text; capacity is launch-probed with a 4,096-entry ceiling. General short text bypasses hashing; changed content or encoding is recounted. Provider-reported per-conversation anchors use a separate launch-probed LRU (128 fallback, 4,096 distributed, 8,192 hard ceiling): TTL/capacity/pressure eviction falls through to the next counter, retained fields are bounded, and expiration plus replacement are atomic.

Child invocations use `compaction_owner_key(task)` for provider-usage cache writes and native-compaction invalidation; parent `convId` is authorization lineage, never the child's cache key. The common settlement helper folds paid summary calls into `usage` and `apiRounds` before child/root terminal branching (and is shared by failure/cancellation). Drained costs are retained on the task until projection succeeds, so a cleanup fault cannot silently discard them. Child teardown independently releases token-cache and summary-cooldown entries, including after `KeyboardInterrupt`, without clearing parent or sibling state. Completed children retain no global usage/cache/cooldown scope; durable archive retention remains the storage owner's separate lifecycle. Specs: `tests/tasks_pkg/test_child_compaction_lifecycle.py`.

L2's request gate counts tools; its message-only estimate retains retry/archive/analytics contracts. Final post-injection admission treats that canonical complete-request count as its total exactly once and passes it call-locally to the body clamp; v2 schema/message fields only decompose it. Missing or invalid evidence rescans. Mutation invalidates reuse; current-task cache evidence wins after two misses.

Within the request gate, the broad heuristic prefilter is lazy. A usage-cache hit or successful local tokenizer returns without paying for a discarded request-wide heuristic scan. Network counters still use that prefilter at the same threshold, and a later heuristic fallback reuses the already-computed value. A usage-cache hit verifies the complete measured-prefix digest, exact model, and system/tool envelope without copying the historical message list or re-tokenizing the prefix. A sampled tail is not sufficient evidence: earlier edits and cold history reconstruction invalidate reuse. On a miss, historical provider usage must never cap a fresh count. Native post-compaction effective usage cannot authorize the original pre-compaction message list.

The selected-slot body adapter adds a local-only hard-window check after route adaptation. It recounts messages, system and tool schemas locally using bounded content-digest tokenizer caches; unbound integers and same-model hints cannot skip this safety check. Over-window input raises the existing typed context-limit failure before transport so the task can compact and retry; the adapter never silently truncates user history. Opaque reasoning ciphertext length is not text-token usage. Provider-native counts remain authoritative for billing.

Adaptive L2 compaction is opt-in. It compares route-priced, cache-adjusted input savings with compaction-call and evidence-loss cost; context-window safety remains an unconditional hard gate. Generated state is checked against pending work and the evidence ledger; failed validation preserves the original context for live/transient tasks. Only explicit utility callers may retain a deterministic bounded view. `remainingRoundsMedian` is also the horizon for exact pre/post-summary checks. Fixed compaction starts at one round, then uses the shortest of the four most recent successful compaction gaps and the current window's observed age, capped at six and by the remaining API-round budget. Long task age cannot hide a three-round prefix lifetime.

An automatic economic decline records the optimistic token-growth lower bound at which the candidate could first repay its cache rewrite or meet the minimum reduction. Until that bound, later rounds reuse the preflight veto instead of rebuilding the same fold candidate. A lower warm-cache witness or a longer observed compaction window invalidates the veto immediately; explicit/reactive compaction and the hard window gate always bypass it.

Pre-compaction archives remain exact audit snapshots. Metadata/summary loads without the potentially multi-megabyte message payload; clients load messages only for explicit inspection, copy, or download. The bounded `tofu.compaction-receipt/v1` records strategy/fallback, preserved anchors/turns/tool rounds/files, durable-objective application, summary time, normalized usage, cache payback, evidence, and recovery truncation. Stored with the archive, it never enters model context or enlarges the next request.


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


Summary credential rotation reads its selected model from declared dispatch
options. Standalone/manual summaries can have no pinned model; an unassigned
live-task local must never prevent their first request. Streaming, nonstreaming,
completion verification and credential-rotation tests cover both entry shapes.
