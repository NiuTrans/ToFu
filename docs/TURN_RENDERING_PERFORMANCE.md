# Turn rendering performance

Problem: stream events previously copied entity indexes, reselected the whole
conversation and rebuilt a shell fingerprint covering every loaded Turn. Even
a small DOM update repeated work proportional to historical conversation size.
Decision: the reducer owns immutable changes; the view-model selector and shell
reconciler consume the same reducer receipts. Existing scheduling, TurnRuntime,
ConversationSurface, renderer, transport and settlement owners remain in charge.

`core/turn-projection.ts#createTurnProjectionReconciler` retains one immutable
state reference and a Set of lane-member IDs per bound conversation. For a
contiguous reducer receipt with unchanged lane order, it compares only changed
members plus the shell-visible phase. It does not hash a partial transcript.
Missing/reset receipts, lane changes, an invalidated owner key or a missed
transition use the complete existing fingerprint. Revision/count adoption,
transport notices, stale DOM repair and every existing side-effect callback
retain their previous semantics. The legacy stateless adapter remains exact.

The conversation's `_turnProjectionFingerprint` is an opaque invalidation key
on the incremental runtime path; it is neither a content digest nor acceptance
evidence. The memo is published before callbacks so reentrant projection sees
the current owner. `disposeConversation` forgets the bound conversation; the
WeakMap does not retain otherwise unreachable owners. Membership scales with
loaded history, not streamed-event count. An active conversation also need not
scan history to determine whether its transport should remain connected.

Alternatives rejected: rebuilding complete strings repeats linear work;
commutative/probabilistic hashes can hide changes; an additional event log or
mutable transcript duplicates authority; suppressing catalog/terminal callbacks
or delaying correctness checks to improve a timing changes behavior.

The browser fixture in `tests/support/turn_pipeline_browser.ts` sends valid wire
events through the actual sync coordinator, TurnRuntime, scheduled controller,
selector, rich Markdown renderers and Surface. The real Chromium MutationObserver
records when the expected content reaches DOM. The shipped stylesheet and the
fixture's extracted production renderer CSS both load. It separately records
dispatch work, scheduled render work, hydration-to-first-DOM, a 20-event burst,
terminal state, retained reader node/focus/selection and full DOM/text digests.
Network input alone is synthetic; production owner sources are not rewritten.
The UI shell's catalog/navigation callbacks, backend, network, startup and
physical pixels are outside this fixture. DOM timing is not paint timing.

Run `pytest -q tests/visual/test_visual_turn_pipeline_performance.py` with a
working Playwright Chromium. `TOFU_TURN_PIPELINE_OUTPUT=/absolute/directory`
retains raw timings and bundle/style hashes. For paired runs,
`TOFU_TURN_PIPELINE_BUNDLE=/absolute/fixture.js` loads a compatible fixture built
with `scripts/vite_test_bundle.mjs` against a separately prepared source graph;
load its extracted CSS too. Keep the fixture identical, preserve unrelated
changes, alternate baseline/optimized order, and compare DOM/text digests before
interpreting clocks. Timing thresholds are intentionally absent from CI.

Functional guards are `core/turn-projection.test.ts`, the runtime perception and
revision-adoption tests, and the streaming reader/browser suites. A local Node
retained-heap diagnostic with 20 independent 4096-Turn conversations measured
about 203 KB per conversation for full fingerprint strings versus 83 KB for the
memo after an event; forgetting owners released about 82 KB each. This excludes
shared TurnState and DOM memory and is not a browser whole-application budget.

A paired Chromium 151.0.7922.34 fixture (64/512/4096 loaded Turns; 60 measured events after
four warm-ups; baseline → optimized → optimized → baseline) isolates the reducer,
selector, entity-index and shell improvements while preserving concurrent source
changes. Every paired DOM/text digest and renderer/style hash matched. For 4096
Turns, the two baseline/optimized runs measured:

| Client metric | Baseline | Optimized |
|---|---:|---:|
| Event callback to observed DOM p95 | 21.6–22.2 ms | 17.9–18.1 ms |
| Synchronous event dispatch p95 | 4.2–4.4 ms | 0.6–0.7 ms |
| Scheduled selection and DOM work p95 | 4.9–5.6 ms | 1.2–1.5 ms |
| 20-event burst to observed DOM, one commit | 71.8–72.5 ms | 17.7 ms |
| Burst synchronous dispatch work | 68.0–68.6 ms | 5.2–5.3 ms |
| Hydration callback to first mounted DOM | 427.6–432.0 ms | 137.5–141.3 ms |

At 64 and 512 Turns, DOM latency remained around one browser frame (17.7–18.1 ms);
these small-case DOM clocks do not support a large perceived-speed claim. The
separate runtime-shell change reduced the already optimized 4096-Turn burst's
observed dispatch work from about 37 ms to 5 ms. These local diagnostics establish
scope and tradeoffs, not a device-independent latency promise or startup claim.

## Swarm receipt projection work

Problem: each mirrored agent receipt reparsed every tool output; cross-Turn
receipt propagation also cloned every full projection once per receipt. These
operations ran synchronously before the scheduled Surface commit, so DOM
coalescing alone could not prevent a frozen tab. A recovered production frame
lasted 11.8 seconds; isolated browser replay reproduced a 5.2-second event callback.

Decision: `presentation/agent-message-receipts.ts` collects evidence before
mutation, coalesces exact run/agent/message identities with the existing monotone
merge rules, and indexes senders/run owners once per batch. Output parsing is at
most once per round per batch. `application/swarm-presentation-overlay.ts` enriches
cross-Turn messages in one batch, copying each candidate Turn once per pass,
independent of receipt count. Inputs remain immutable; confirmation, earliest
accepted timestamps, explicit bodies, terminal evidence and run isolation remain
intact. Indexes are call-local and released after projection; no timer, retained
transcript cache, transport or durable state is introduced.

Alternatives rejected: throttling rendering leaves synchronous ingress blocked;
dropping history/receipts loses evidence; caching serialized transcripts retains
large duplicated bodies without removing the repeated work.

One local paired replay of the same historical snapshot preserved the complete
projection and reduced a full browser event callback from 5,197 ms to 116 ms.
These are diagnostic measurements, not latency guarantees. The synthetic Chromium
fixture adds 148 large tool outputs and 32 mirrored receipts to retained history,
then verifies live updates, a coalesced burst, terminal delivery and reader
focus/selection. Work-count regressions bound transcript copying and parsing
independently of mirrored receipt count, without host-sensitive timing gates.
Specs: `application/swarm-receipt-pressure.test.ts`, the existing message receipt
and swarm authority tests, and
`tests/visual/test_visual_swarm_receipt_responsiveness.py`.

## Complete application delivery

`tests/visual/test_visual_live_pipeline.py` now drives the shipped application,
selected disposable storage, ordinary root FIFO, production text coalescer and
v4 SSE. Only provider text is synthetic: 40 paced chunks followed by a 20-chunk
burst, with zero or 256 stored historical Turns. It verifies exact durable and
reloaded content, trusted composer input, retained draft/focus and no hot snapshot
refetch. Passive observers record post-ACK notifications, real SSE frames, DOM
arrival, input-handler-to-animation-frame intervals and supported long tasks.
Nine round-trip clock bounds before and after the run qualify clock alignment.

Run `TOFU_LIVE_PIPELINE_OUTPUT=/absolute/directory pytest -q
tests/visual/test_visual_live_pipeline.py` with a verified engine for the configured
backend. `python -m benchmarks.live_pipeline receipt.json` recomputes summaries;
its guard refuses incomplete producer/commit/SSE/DOM evidence or clock drift.
Set `TOFU_TURN_DELTA_RECORD_MS=300` for the retained fixed-window policy control;
set it to `0` to measure eager projection using the existing explicit override.
The default remains 300 ms. Use fresh serial processes in alternating
order, identical built assets/work and complete cleanup. Keep backend cohorts
separate. Failed exploratory runs and their causes remain in the run evidence.

These are instrumented local diagnostics. Notification callbacks can race wake
subscribers; they are not native commit/fsync timestamps. Serialized command and
SSE sizes measure logical transport, not physical WAL/storage amplification. DOM
and animation callbacks do not establish displayed pixels or complete input INP.
The fixture excludes real inference, two-hour workload quality and device-wide
performance admission. Long history is viewport-windowed by the actual UI.

An alternating four-process native embedded experiment on Chromium 151 compared
the fixed window with a candidate default that used root FIFO backlog batching.
All eight browser cases preserved exact content, draft/focus and reload results.
Across zero/256 stored historical Turns, text-to-DOM p95 fell from 297–300 ms to
27–34 ms; post-ACK-notice-to-DOM p95 stayed about 26–32 ms. Each case still made
42 storage commands. Logical command bytes grew from 90,085 to 102,544 (+14%);
SSE bytes grew from about 68.7 to 95.5 kB (+39%). Native WAL
buffer counter deltas grew from 0.39–0.59 MB to 0.75–1.05 MB, and measured
application-process CPU rose from 104–230 ms to 183–506 ms. These process-level
counter windows include background work and do not establish per-command I/O
attribution or stable disk footprint. Input-handler-to-frame p95 stayed below
16 ms, with no observed long task in the measured streaming interval.

Decision: withdraw the candidate default; retain the existing operator override
and this measurement. Alternatives rejected: reporting only the latency win
hides physical write and CPU growth; treating unchanged command counts as equal
storage work misses projection/replay amplification. Further default changes need
controlled concurrency and physical-write evidence alongside the latency result.

Concurrent producer coverage uses `TOFU_LIVE_PIPELINE_CONVERSATIONS=N` (default
1; the diagnostic accepts 1–64). Each participant owns an ordinary conversation,
task, attempt, production coalescer and FIFO. A shared start gate releases the
producers. All completions, delivery receipts and exact persisted bodies are
checked before reporting; CPU measurement ends after every producer is joined.
Only the foreground conversation has the measured browser/SSE subscriber and
the requested historical Turns. This is shared-storage producer contention,
not a claim about N browser clients or N concurrent model invocations.

The receipt retains every participant's production/completion timestamps and
scoped command observations. Summaries require overlapping producer lifetimes,
complete populations, matching content, drained delivery and commands inside
the measured window. Foreground command counts remain separate from cohort
counts; process CPU and WAL deltas cover the whole cohort plus incidental
background/instrumentation work. An absent or unfinished peer cannot silently
reduce the measured cost. Older single-conversation receipts remain readable
without being assigned cohort evidence they did not capture.

A same-build native ABBA diagnostic with eight producers compared 300 ms and
80 ms windows (two samples per arm/history size). Foreground text-to-DOM p95
was 296–315 versus 101–110 ms; input-handler-to-frame p95 stayed below 16 ms,
with no observed foreground long task. At zero/256 historical Turns, median
process CPU rose 23%/14% and WAL-window bytes rose 53%/17%. All participants
completed exact content. The existing coalescer batches more work under load,
so cohort command counts differ even with identical final content.
Decision: retain the default window; 80 ms is a measured latency/resource
tradeoff, not reduced write amplification. Reporting only foreground costs
was rejected because storage/CPU are shared. This small diagnostic does not
admit production capacity or real-model task quality. Matched identities,
raw-receipt digests and per-leg observations are in
`tofu_live_pipeline_concurrency_20261015.json`.


## Provider text and committed projection cadence

The durable Python provider-text entry now shares the existing committed
projection deadline with its bounded text coalescer. The 300 ms projection
policy and 100 ms maximum text window remain. First text and structural/close
flushes retain their original order. The character flush hint uses the existing
transport/progress envelopes rather than forcing every 256-character chunk
through delivery. The policy owner and alternatives are in
[task event delivery](TASK_EVENT_DELIVERY.md#provider-text-cadence).

An actual-code ABBA comparison used four fresh processes, eight concurrent
synthetic producers and one Chromium browser, with identical shipped assets
and verified native embedded storage. Two samples per arm/history retained all
exact content, reload, draft/focus and ordered delivery assertions. Median
results for the complete producer window were:

| Stored historical Turns | Application CPU, before → after | WAL-buffer bytes, before → after | Foreground text-to-DOM p95, before → after |
|---|---|---|---|
| 0 | 629.2 → 380.5 ms (−39.5%) | 3,289,229 → 2,403,797 (−26.9%) | 298.0 → 287.0 ms |
| 256 | 620.1 → 448.1 ms (−27.7%) | 3,788,628 → 2,935,658 (−22.5%) | 306.5 → 299.0 ms |

CPU/WAL include background and instrumentation work. Resource sampling covers
the same application process: peak-minus-start RSS was 27–45 MiB before and
27–30 MiB after, while median peak-minus-start thread growth increased by 6.5
threads in each cohort. The existing coalescer worker is now used by larger
chunks; every producer reported it joined with no pending text or failed emit.
Observed candidate buffer peaks were at most 11,286 characters per producer.
These sampled process values are not allocation attribution or OS high-water
marks. No foreground long task was observed; input-handler-to-frame timing,
first-text observations, raw receipt digests and source/asset identities remain
in `tofu_provider_text_cadence_20261016.json`.

Decision: use aligned batching for this provider-text entry. A size-only 4,096
character candidate increased p95 by about 50 ms and was rejected. One earlier
profile-on window dropped samples and was excluded from cost attribution;
performance pairs ran without that profiler. These small local samples cover
the declared synthetic workload, not production capacity, native model ingress,
real inference, device-wide performance or long-task acceptance.

## Native ingress browser evidence

Problem: the Python synthetic producer does not exercise the native text ACK
or terminal writer, and input during model preparation does not prove input
responsiveness during text delivery. Decision: the same live-pipeline browser
fixture accepts `TOFU_LIVE_PIPELINE_PRODUCER=native` with an explicit verified
`TOFU_NATIVE_TERMINAL_TEST_ARCHIVE`. Its paced loopback HTTP provider feeds real
native clients through `run_native_task`, the canonical text/terminal owners,
selected storage and ordinary v4 SSE/DOM. All participants must retain exact
text hashes, ordered native callbacks, completed delivery and joined clients.
Typing starts after visible partial text and before the complete response;
`streamInputHandlerToFrameMs` excludes input before the provider begins or after
the final DOM observation. Missing overlap cannot produce a responsiveness result.

The fixture's independent archive clients include context preparation and
terminal work. It does not model the public ROOT admission or shared production
NativeHost lifetime. Process CPU includes the fixture; waited-child CPU is a
separate process-wide observation, and sampled parent RSS excludes native-child
memory. An unsettled WAL window remains unqualified. The Python producer is a
regression check, not equivalent work for a native/Python speed comparison.
Native callback counts may differ from HTTP chunk counts; exact concatenated
UTF-8 content, sequence and completion are the invariants.

Alternatives rejected: relabeling direct Python callbacks as native execution,
using final text alone to prove streaming, or copying Python batching settings
before measuring native ACK/lifecycle costs. This evidence changes no native
batching, admission or cancellation policy. Tests: `test_live_pipeline_measure.py`,
native text/task execution contracts and `test_visual_live_pipeline.py`.

Two fresh native diagnostic processes each ran eight concurrent producers with
0 and 256 stored historical Turns. All four cases observed partial text before
typing, retained all exact content after reload, and joined all 32 client runs.
Text-to-DOM p95 was 296–312 ms; input-handler-to-frame p95 during streaming was
15.2–15.3 ms, with 38 input samples per case and no observed foreground long task.
The original 300 ms projection policy remains. Source/archive/asset identities,
raw receipt digests and scoped resource observations are in the
`tofu_native_live_pipeline_20261016.json`.


## Native interaction observations

Problem: an input-handler-to-animation-frame clock omits the input queue and
cannot establish the browser's next-render duration. Decision: the same complete
application fixture additionally observes browser Event Timing at a 16 ms
threshold, preserving its 8 ms duration granularity and grouping reported events
by interaction ID. A bounded delayed trusted key calibrates the observer before
the workload; it is excluded from the measured CPU/stream window. Native key
counts must match the trusted composer-key population, and dropped observations,
failed calibration or missing overlap refuse qualification.

Event Timing omits events below its threshold. Reported durations describe that
observed subset; an empty subset remains absent, not zero latency or a complete
INP score. Scroll wheel events are outside Event Timing, so trusted upward/downward
progress and scroll-handler-to-frame observations remain separate. The existing
exact content, durable reload, draft/focus, complete producer population and
selected-store checks still apply. Alternatives rejected: relabeling animation
callbacks as next paint or inserting zero durations fabricates unobserved work;
changing batching/rendering policy would confound the diagnostic extension.
The timing definitions follow the [W3C Event Timing working draft](https://www.w3.org/TR/event-timing/).

The fixture closes the explicitly requested setup dialog through its real close
control before calibration. Scroll progress uses successive observed positions
after the trusted wheel timestamp: content may grow while the pointer moves,
and compositor scrolling may precede the passive main-thread wheel callback.
Neither clock's position is a reliable pre-scroll baseline. Raw failed receipts
remain available; new receipts qualify only after owned fixture cleanup succeeds.

`TOFU_LIVE_PIPELINE_HISTORY=256,4096` selects explicit stored-history populations;
the default remains `0,256`. This does not force every stored Turn into the
viewport or change application pagination. Use fresh processes and frozen assets,
report storage and producer cohorts separately, and retain failed exploratory
runs. These diagnostics exclude physical display latency, field INP, real model
quality, long-task recovery and production-capacity admission. Guard coverage is
`tests/meta/test_live_pipeline_measure.py`; the actual browser producer remains
`tests/visual/test_visual_live_pipeline.py`.

Two fresh native-storage application processes passed the default 0/256-history
cases with eight concurrent Python producers. Each recorded 38 trusted keys;
33–38 interactions per case were reported above the timing threshold. Reported
interaction-duration p95 ranged from 32 to 48 ms; text-to-DOM p95 was 277–288 ms.
All four cases retained draft/focus, exact durable content and owned cleanup;
the history cases also observed trusted upward/downward scroll progress. These
are current-source observations, not an optimization comparison. Source, asset,
receipt identities and failed explorations are retained in the
`tofu_browser_interaction_20261016.json`.

The original 4096-history extension failed owned cleanup in both exploratory
processes: native `conversation.delete` returned `database_resource_exhausted`.
Its former eager event retirement shared the 2000-row identity collector limit.
The earlier collector wrote timing
summaries before cleanup; those scenarios remain failed and cannot qualify the
larger-history boundary. The current collector publishes a qualified summary
only after cleanup. Those historical failures remain failed observations.

Native deletion now pins exact identity sources and delegates bounded event
reclamation to existing maintenance, as specified in
[canonical event lifetime](STORAGE_EVENT_REPLAY.md#deleted-conversation-event-lifetime).
Two fresh application processes using the verified candidate binary/library
passed all six 0/256/4096-history cases with eight concurrent synthetic Python
producers, including exact content, draft/focus, scroll and owned cleanup.
Native regressions separately cover 97 nonempty event ranges, restore, cursor
reopen, identity reuse and partial/final cleanup filesystem faults. A matched
daemon work-count probe with 97 Turns of approximately 64 KiB content measured
6,398,259 collected/decoded blob bytes during old foreground deletion and zero
in the candidate. Deferred cleanup still performs bounded background work;
this is not a whole-system cost or latency speedup claim. Source/build identities,
counts and scope are retained in the
`tofu_conversation_event_retirement_20261016.json`.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
