# Context compaction policy

This guide owns the detailed compaction policy referenced by the
[context-engineering domain map](modules/context_engineering.md).

Automatic compaction prepares a bounded request view without rewriting durable
turns. Manual compaction explicitly mutates the conversation by writing a
persistent summary boundary through its authority, guarded against intervening
turns.

The per-round L1 pass is isolated from transcript authority. It receives an
API-form request projection, replaces cold tool/image bulk only in that working
copy, and performs no conversation read, projection update, or change
notification. The next user turn rebuilds from unchanged settled Turns and may
derive the same bounded request projection again.

Both paths may share summarization/token-budget primitives, not persistence
semantics: a request-local summary is not the stored transcript, and persistent
compaction never bypasses the conversation command service.

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
fields for old consumers. This keeps old readers compatible without creating a
second stored representation of the summary boundary.

The first real user anchor and required system/project policy remain available
after compaction. Tool-call/result pairing is derived only from the adjacent
assistant/result run; duplicate IDs pair by occurrence. Orphans remain
unpaired, so a later success can never authorize or relabel an earlier call.

L1's default tool-result hot tail is the intersection of a 40-result ceiling
and a 48K estimated-token ceiling. It never edits the warm cache prefix and
always protects the newest complete tool-call batch, even when that batch alone
exceeds 48K. Older reconstructible results become request-local placeholders;
their durable tool results and artifact references remain untouched. The
opt-in `adaptive_hot_tail` method remains a separate experiment rather than a
second default authority.

Automatic L2 and manual `/compact` select the newest contiguous complete
tool-round suffix under the same preservation token budget used for turns. The
configured hot-round count is a maximum, not an unlimited entitlement: an
oversized recent read is folded while the newest complete call/result pair
remains recoverable. Their summary is a compact state receipt (objective,
binding constraints, verified work, current state, blockers, next steps); the
objective and a bounded recent instruction set are retained verbatim outside
that lossy receipt, and reconstructible `data/tool-results` paths are never
promoted to durable working files. The model targets 800–1,600 receipt tokens
with a hard 2,200-token dispatch ceiling.

The receipt's **Objective** is model-authored from verbatim goal evidence: the
elision policy never drops user turns, and the earliest-request anchor (pulled
from the lossy region for verbatim reinsertion) is re-supplied to the summary
model, which writes the current effective goal itself. A replaced or completed
opening ask is history; a status question, error fragment, or correction is
steering under blockers/next steps, never the Objective. Code only guarantees a
non-empty Objective (anchor fallback on miss). An accepted receipt re-pins the
autopilot objective pin on goal change; `_isMeta` rows never become a human
objective.

Proactive cache economics include a conservative summary-call estimate before
dispatch. Current and candidate prompt prices are resolved independently, so a
provider-declared context-tier change affects the calculation without a model
special case. Recurring savings compare the observed warm prefix before and
after the rewrite; uncached tail tokens are not assumed to become cache reads.
Once a summary has been generated, that cost is sunk and adoption compares only
the future prefix rewrite with future input savings. Archive and UI token
counters are explicitly heuristic (`tokenCountKind=estimated`), not
provider-billed usage.

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

Each automatic L2 preflight has two measurements: the request gate includes tool
schemas, while the message-only estimate retains retry/archive/analytics/reminder
contracts. Final post-injection admission treats that canonical complete-request
count as its total exactly once and passes it call-locally to the body clamp; v2
schema/message fields only decompose it. Missing or invalid evidence rescans.
Mutation invalidates reuse; current-task cache evidence wins after two misses.

Within the request gate, the broad heuristic prefilter is lazy. A usage-cache
hit or successful local tokenizer returns without paying for a discarded
request-wide heuristic scan. Network counters still use that prefilter at the
same threshold, and a later heuristic fallback reuses the already-computed
value. A usage-cache hit verifies only the bounded recorded-prefix tail instead
of copying the complete historical message list.

Adaptive L2 compaction is opt-in. It compares projected Kimi cache-adjusted
input savings with compaction-call and evidence-loss cost; context-window safety
remains an unconditional hard gate. Generated state is checked against pending
work and the evidence ledger; failed validation retains a deterministic bounded
view. `remainingRoundsMedian` is also the horizon for exact pre/post-summary
checks. Fixed compaction starts at one round, then uses the shortest of the four
most recent successful compaction gaps and the current window's observed age,
capped at six and by the remaining API-round budget. Long task age cannot hide
a three-round prefix lifetime. Adaptive candidates are no longer admitted then
silently vetoed by a fixed gate.

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
