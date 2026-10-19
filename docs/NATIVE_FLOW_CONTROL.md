# Native Flow control ABI

`contracts/native_flow_abi_v1.json` defines the feature-gated `flow-abi` runtime
library interface, status/action/query vocabulary and record layout. Generate
with `scripts/gen_native_flow_abi.py`; check mode rejects drift. Build the pure
library with `cargo build -p tofu-runtime --lib --release --features flow-abi`.
NativeFlowLibrary snapshots a trusted explicit path into sealed code after exact
SHA-256 verification, validates ABI identity, and borrows no storage driver.
The caller supplies its capacity and owns the library lifecycle; application
policy borrows it through NativeRootConfiguration.flow_scheduler. Each graph
carries explicit owner, tenant and task identity and owns immutable topology;
monotonic graph/cursor handles refuse cross-graph, stale and duplicate use.
Caller buffers remain alive/aligned through synchronous return. Invalid queries,
short outputs and rejected transitions publish no result or successor.
Close withdraws and joins accepted calls before release; live cursors prevent
graph close and graph borrowers prevent code unload. No resident thread, queue,
cache or journal is added. Retained graph storage scales with nodes/edges and
cursor storage with active walks. Query scratch is released with each call.
A fresh-process Linux probe loads 1,081,456 code bytes and adds one descriptor.
RSS changed from 29,468 to 30,660 KiB on load, then 32,624 KiB with a 1,000-node
graph and 64 cursors; close restored descriptor count and observed 32,472 KiB.
These include Python projections and loader/allocator residency, not proof of
full RSS return. No new resident execution concurrency is introduced.
Library packaging/automatic discovery and production-default admission remain
pending. This explicit library is executable operator input, like the current
native token ABI; a digest is integrity evidence rather than publisher identity.
Verification: native Flow engine/topology/lifetime/recovery tests, real v4
native-graph/role fixtures and the Rust workspace with `tofu-runtime/flow-abi`.


The optional tofu_flow_cursor_transition capability acknowledges the returned
action and obtains its next action under one native lease and cursor lock.
Host abort checks still precede effect callbacks and continuation. Rejected
successors or invalid output buffers preserve the pending action. A next-action
budget failure acknowledges the already returned effect and cannot reissue it.
Old pinned ABI-v1 libraries retain their existing advance/next calls inside one
host lifetime guard; selected execution remains native in either case.
Problem: separate acknowledgement and next lookup doubled cursor crossings.
Decision: combine those native operations without moving callbacks or journal
ownership. Alternatives: prefetching effects weakens acknowledgement order;
ignoring next-action errors hides structural failure; replacing the old ABI
would unnecessarily reject previously pinned libraries.
A 1,000-action diagnostic records 2,001 versus 1,001 native cursor calls with
identical action order. Eight interleaved complete HTTP requests per library
measured combined/legacy median 1.013; observer polling and provider/storage
work are included, while library/application setup is excluded. These small
samples do not establish a system performance advantage or qualify P99/default
promotion; raw samples, including first-arm outliers, are retained.

The optional `tofu_flow_classify_verdict` capability evaluates verifier feedback
in Rust: last-tag precedence, worker/planner distinctions, unresolved STOP
downgrades, virtual-user blocked/completion progress, and feedback cleanup.
Problem: a native cursor still crossed into Python for every verdict decision.
Decision: generate patterns, blocklists and Unicode classes from the existing
`lib/agent_verdict` authority with `scripts/gen_native_verdict_policy.py` under
project Python 3.12 (Unicode 15.0.0); check mode refuses a different Unicode
version or stale output. Alternatives: independent Rust regex rules diverge on
word boundaries/case/decimal digits; a Python callback retains the decision
owner. The host supplies replan policy and integer digit limits and emits the
original audit fields through the public verdict facade. Graph identity and
joined library lifetime remain mandatory. Invalid UTF-8/flags publish nothing;
a short buffer publishes only required bytes, never a decision or audit.
Pattern compilation is lazy, shared for the loaded library and released with
code unload; there is no background worker or retained feedback. First-call
compilation and library residency must be measured separately from warm calls.
Older explicitly pinned libraries without this optional export retain Python
classification; selected native classification errors propagate without retry
through Python. This compatibility boundary is not full native-policy admission.
Native HTTP verifier-loop tests prohibit the Python classifier, preserve four
original child invoices and shared usage, and join completion. Child invocation
also preserves explicit `preferencesEnabled`: My Context is independent from
`memoryEnabled`; delegated work cannot silently re-enable a disabled capability.
These correctness checks do not qualify whole-system latency or default changes.

Diagnostic measurement of the classifier showed initial regex compilation at
21.716 ms and 13.228 µs per warm host call versus Python's 3.066 µs.
Replacing expanded regex lookarounds with binary searches over generated Python
word ranges reduced generated rules from about 329 KB to 31,196 bytes and first
classification to 2.355 ms. Warm host calls remained 13.087 µs versus 3.108 µs
(4.211×), including lifetime guards, C ABI and JSON transport. These are small
feedback diagnostics; retain this regression rather than infer Rust admission.
The optimized probe loaded 3,872,208 code bytes; RSS was 35,664 KiB before,
39,004 loaded, 40,092 after lazy compilation, and 39,996 after close. One added
descriptor returned to baseline. Allocator/loader residue prevents a full RSS
return claim. Larger-policy batching and transport costs remain optimization
work; no system performance gate has changed.

The final regenerated artifact repeated this diagnostic: first classification
2.330 ms, warm ratio 4.171× Python, code 3,872,224 bytes. RSS before/load/compiled/
closed was 37,860/41,032/42,248/41,708 KiB. Raw samples and exact artifact hashes
are retained in the verifier-policy evidence; neither repetition passes a
whole-system gate.

Generated Unicode identity is also checked at library admission. A host Python
with different Unicode tables refuses the verifier-capable library before any
role dispatch and releases its code descriptor; it cannot silently classify
with different word/decimal rules. Libraries without this optional capability
retain their existing graph-only admission.

The optional `tofu_flow_loop_{open,advance,close}` capability moves loop
iteration, challenge, replan and checkpoint decisions into a graph-scoped Rust
state. The host borrows detached producer/transcript/ledger observations and
executes returned ordered directive/event/body/replan effects through existing
owners. Native classification and decimal progress parsing are included in the
transition; Python no longer decides those gates for a selected native loop.
Large progress counts cross as decimal strings, including negative checkpoint
deltas; regression cannot erase previously credited progress. The existing
feedback ledger remains the run projection and shared-context owner.
Problem: native verdict parsing alone retained a separate Python loop state
machine. Decision: native state selects every loop continuation and returns
effects; directives/constants derive from retained policy through
`scripts/gen_native_loop_policy.py`. Alternatives: independent hardcoded
directives drift; host retries after an ambiguous transition repeat effects;
moving provider/journal authority into this library creates a second owner.
Each transition checks operation and iteration against its state. Malformed
frames and short buffers leave state unchanged; short buffers publish only
required byte count. Loop handles prevent graph close and withdraw/join before
release, as cursors do. Partial capability sets are refused at library admission.
Older pinned libraries retain explicit Python loop policy; selected native
errors never enter it. Cancellation before the next iteration publishes no next
iteration event or body. Existing role effects and journal replay remain owned.
No resident worker, queue or durable growth is added. Fixed state retains only
loop identity/counters/challenges and the latest credited decimal checkpoint;
per-call feedback/ledger scratch is released, with capacity derived from actual
input rather than a fixed machine assumption.
Verification includes full outcome/event/directive/ledger/audit differential
matrices, long decimal progress, regression, cancellation, scoped/concurrent
handles and short-buffer retries, plus actual v4 loops and recovery.
A pre-Unicode-fix five-iteration diagnostic (including graph setup, without
provider/storage) measured native/Python 4.618×: 440.300 versus 95.348 µs.
First native loop was 3.533 ms; 3,995,024 code bytes loaded. RSS before/load/
working/closed was 47,976/50,724/52,324/49,928 KiB; the added descriptor returned
to baseline. Allocator/loader residue prevents a full RSS reclamation claim.
The same pre-fix artifact, in eight HTTP loops per arm, measured median 374.075 ms native
versus 374.542 ms prior (ratio 0.99875); polling/provider/storage and first-arm
outliers remain in the raw observations. Library/application setup is excluded.
Neither diagnostic nor these small HTTP samples establish a performance
advantage or qualify whole-system P99/default promotion. Cross-language frame
costs and the remaining native callback ownership still require optimization.
The checkpoint-only no-default-features build verifies normalized Chat values;
exact provider insertion-order assertions apply when provider-json-order is
enabled, as its declared contract requires.

A further differential exposed Unicode-version drift in Rust standard lowercase:
Cyrillic TJE feedback triggered a strategy nudge absent under Python Unicode 15.
The verdict generator now derives full lowercase mappings and cased/ignorable
ranges from Python, using Python contextual-sigma observations as the oracle.
Native word splitting/lowercase and literal regex case classes use those rules,
including final sigma and Python's four extra ASCII-ignorecase characters.
A 3,000-string Python context oracle plus the full classifier/loop matrix passes.
The corrected artifact measured 4.908× Python in the same small five-iteration
diagnostic, first loop 3.394 ms, code 4,063,688 bytes. RSS before/load/working/
closed was 45,072/48,176/49,840/47,404 KiB and the descriptor returned to baseline.
Both raw diagnostic cohorts remain; the slower result is not discarded.

The corrected artifact repeated the full HTTP comparison: native median
374.891 ms versus prior 376.184 ms (ratio 0.99656), eight samples per arm.
Both cohorts and all outliers remain in evidence. This small difference grants
no performance or production-default admission.

Loop transport reuses a loop-owned output buffer: it starts at 4 KiB, grows to
the required frame size, copies only published bytes, and releases at joined
close. Input bytes are borrowed unchanged through synchronous return, avoiding
a second ctypes allocation/copy. Each loop serializes buffer users; close
withdraws new host calls before joining its active frame and never reuses a
buffer while decoding is pending. Accepted failed frames still leave native
state unchanged. The encoder configuration is shared; each encoding owns its
local recursion state. No worker, queue, persistent cache or concurrency cap
is added; retained transport capacity scales with actual per-loop output.
The optional `tofu_flow_loop_advance_text` export requires null
`justification_chars` and computes blocked-evidence character counts in Rust
only after the earlier evidence gates require them. Token removal and blank
husk collapse derive from the same Python registry/generator as verifier rules;
counts use Unicode characters, not UTF-8 bytes. The older advance export keeps
its caller-computed integer semantics. Nullable/integer frame mixing refuses
before transition. Required loop symbols are an explicit contract group so
previous libraries are not mistaken for incomplete new capabilities.
Problem: every iteration allocated/copied buffers and cleaned tokens even
when no blocked claim existed. Decision: reuse scoped transport storage and
derive text evidence at its native decision point. Alternatives: sharing one
buffer across concurrent loops corrupts responses; omitting joined lifetime
checks risks code unload; caching arbitrary feedback adds retained user data.
Eight alternating fresh-process diagnostic cohorts measured candidate/prior
median 0.88646 (406.996 versus 459.124 µs per five-iteration loop), retaining all
samples. This is an 11.35% improvement over the previous native implementation;
it remains slower than the small Python loop and grants no system admission.
Verification covers outcomes/audits, concurrent frames, close withdrawal, large
then small outputs, token-cleanup thresholds, old-library compatibility and
actual v4 role billing/recovery. Production defaults remain unqualified.

The complete HTTP comparison retained a regression: eight samples per arm
measured native-text candidate 385.533 ms versus prior 376.806 ms (ratio 1.02316).
Provider/storage and observer polling are included; library/application setup
is excluded. All raw clocks remain. The diagnostic improvement therefore does
not establish a system latency gain or qualify defaults.


Native parallel composition optionally uses `parallel_plan` plus
`tofu_flow_parallel_merge` from the generated ABI contract. Rust chooses
ordered branches, their structural barrier and the post-barrier continuation;
it concatenates settled branch outputs in definition order with exact empty
text semantics. Host workers still own callbacks, canonical failure envelopes,
abort/event-delivery propagation and joined teardown. A merge failure cannot
fall back or re-execute a branch. Short outputs publish no bytes; malformed
receipts, wrong node types and stale graph handles refuse. Scratch is released
per call; no resident concurrency, handle borrower or persistent state is added.
Problem: topology control remained split and repeated Python concatenation
copied accumulated large branch text. Decision: pure native plan and one sized
merge, preserving existing demand scheduling and durability owners.
Alternatives: a second native worker pool would duplicate lifecycle ownership;
completion-order merge changes the contract; native exception classification
would obscure canonical host failures. Older pinned libraries without the
optional merge export retain their explicit hybrid parallel boundary.
Host diagnostic native/Python ratios were 35.0 for two 32-byte outputs, 4.89
for 32 × 1 KiB, 0.310 for 256 × 4 KiB, and 0.070 for 1,024 × 4 KiB.
JSON, guards and ABI costs are included; these are not whole-request evidence
and do not qualify performance/default promotion. Small merges need further
transport optimization; the measured regressions remain acceptance evidence.
Eight interleaved complete HTTP requests per library measured new/prior
median 1.00655; roles genuinely overlap and original invoices settle once.
Small samples include provider/storage/polling costs and do not qualify P99.
One fresh-process 1,024-branch probe observed RSS 44,848 KiB
before load, 47,768 loaded, 65,804 with a 4 MiB result,
and 48,360 after close. Descriptor and thread counts returned to
baseline; allocator residue prevents a complete RSS-return claim.


Optional `tofu_flow_parallel_merge_spans` borrows caller-owned UTF-8 byte spans
(context first, then ordered branch outputs) for synchronous return. Field
layout and signatures derive from the ABI contract. The host encodes each
string once and retains its bytes; Rust validates every span, graph/node,
branch count, UTF-8 and output capacity before any result bytes are published.
It writes directly into the caller's result buffer without JSON or a second
native text allocation. NULL/unaligned buffers, invalid lengths and malformed text
refuse; short capacity publishes only the required length. Caller memory must
remain valid, immutable and nonoverlapping with writable output until return.
No span, worker, handle or scratch text survives the call. Existing joined
graph/library lifetimes still apply. Older libraries retain their explicit
native JSON merge; a selected span error cannot fall back to JSON or Python.
Problem: JSON transport copied and escaped every branch output before native
concatenation, dominating the linear merge itself. Decision: generated borrowed
spans and atomic direct publication. Alternatives: packed byte frames still
copy all text; Python concatenation retains the policy boundary; per-branch
native state would add lifecycle/receipt ownership without reducing callbacks.
Eight same-library interleaved diagnostic cohorts measured spans/JSON ratios
0.952 (two × 32 B), 0.272 (32 × 1 KiB), 0.148 (256 × 4 KiB), and
0.160 (1,024 × 4 KiB), including host encoding, guards and output decoding.
The separate complete HTTP cohort (eight per library) measured new/prior
median 0.98891. These small samples do not establish P99 or default
admission; small host merges still trail pure Python because of fixed crossings.
A fresh 1,024-branch resource diagnostic recorded RSS 44,564 KiB
before load, 47,484 loaded, 55,956 holding the 4 MiB result,
and 50,512 after close (peak 64,016 KiB). Descriptors and
threads returned to baseline; retained allocator pages remain and this isolated
observation does not qualify whole-application memory/default promotion.


Optional `tofu_flow_branch_{plan,choose}` moves branch successor order,
classifier admission and label selection into Rust. Host projection supplies
one label per unique successor in first-edge order; duplicate structural edges
still count when deciding whether a classifier is needed. The host retains
classifier execution, its failure propagation and the canonical `branch_pick`
event. A selected native error cannot retry through Python. Older explicitly
pinned libraries without both exports retain their existing Python boundary;
a partial pair refuses library admission.
Problem: branch selection retained Python matching and quadratic all-pairs
containment over label occurrences. Decision: borrowed UTF-8 spans, generated
Python Unicode 15 casefold/word/space rules, then an O(M log M) interval sweep
for M matches. Equal spans remain ambiguous; strictly contained spans disappear;
fallback remains the first structural edge. Alternatives: Rust standard Unicode
rules can drift from the host; regex per label retains setup costs; caching user
verdicts adds residency and lifetime obligations without reducing callback work.
Graph leases keep code/state alive through return. Invalid flags, node types,
counts and UTF-8 publish no choice; short output publishes only required length.
No worker, queue, cache or durable state is added; temporary text/match storage
scales with the request and is released before return.
Differential verification covers every changed Unicode casefold mapping,
random overlapping labels, duplicate edges, empty/ambiguous verdicts, failure
propagation, short buffers and stale handles. Real v4 verification reads each
role's distinct visible Turn: the classifier's first Turn keeps its own text,
and the selected role supplies the task's final content. Reading only the first
Turn is not a valid final-content oracle for a multi-role Flow.
Eight interleaved diagnostic cohorts measured native/Python ratios 1.168 for
short text and 1.547 for Unicode text, retaining these regressions. Repeated
nested labels measured 0.001947 at 500 repetitions and 0.000544 at 2,000.
The separate complete HTTP comparison (eight per arm) measured 293.614 ms
native versus 300.780 ms prior, ratio 0.97618, including provider, storage and
observer polling, excluding application/library setup. All observations remain
in `system_native_branch_policy_20261014.json`. Small cohorts do not
establish a system advantage, P99 or default admission; fixed host crossings
still dominate short labels.


The release builder's explicit `--flow` option packages the library and its ABI
contract in the pinned native archive. It can be combined with `--token-count`;
both capabilities share one member and each included contract validates against
its generated host authority. Missing, altered or mismatched contracts refuse
before code loading; a token-only archive cannot satisfy Flow selection.
`NativeFlowLibrary.from_archive` consumes the bounded sealed snapshot directly,
without an extracted install tree or another full library copy. Existing call
leases and graph borrowers still fence unload. Constructor failure closes the
mapping and descriptor; replacing the archive cannot replace admitted bytes.
Problem: manual library paths left graph control outside release admission.
Decision: reuse archive integrity, exact contract and sealed-image ownership.
Alternatives: a second package duplicates release authority; runtime compilation
adds startup work and unpinned toolchains; mutable extraction weakens identity.
Application policy may explicitly select `NativeRootConfiguration(flow_archive=True)`.
It conflicts with a caller-owned `flow_scheduler`. The existing `NativeHost` loads
from its pinned image lazily on the synchronous Flow task-binding lane; ordinary
ROOT binding and idle startup do not load the library. Concurrent initializers
share one lock and one admitted library. Host retirement withdraws new admission,
joins any active initializer and closes graph code through the existing borrower
fence. Failed admission propagates before native invocation publication; failed
retirement remains an application shutdown failure. No worker pool, timer or
additional task/store owner is introduced. Defaults and whole-system performance
qualification remain separate gates.

A fresh-process archive admission probe measured 107.788 ms. RSS was 37,128 KiB
before load, 39,812 loaded/working and 39,028 after joined close. Descriptors
returned from nine to eight and threads to one after the shared executor joined.
Allocator residue remains; this single probe does not qualify application memory
or latency. It demonstrates the lazy library's resident cost and descriptor
lifetime, recorded in `system_native_flow_host_20261014.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
