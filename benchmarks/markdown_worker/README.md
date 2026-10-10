# Browser parser worker experiment

The experiment runs the existing pure source preparation, Marked parser and
strict tokenizer policy in a dedicated worker. The parent uses the real Markdown
renderer and text-block renderer, including DOMPurify, navigation policy, code
projection and forced layout. Parser substitution is an injected test port.
No application runtime, frontend source or rendering authority is replaced.

```bash
python -m pytest -q tests/visual/test_visual_markdown_worker_experiment.py
TOFU_MARKDOWN_WORKER_OUTPUT=/tmp/tofu-markdown-worker.json \
  python -m pytest -q --timeout=600 tests/visual/test_visual_markdown_worker_experiment.py
```

Five workloads (short text, growing prose, code, tables/late references and
terminal rendering) each use three alternating independent browser-process pairs,
100 observations after 20 warmups and separate renderer-memory runs. Unicode,
strict deletion, raw HTML/XSS cleaning, unfinished fences, math, source changes,
superseded queue entries, stale running replies and disposal have browser checks.
The queue contains at most two sources; a stale result never resolves successfully.

The original diagnostic adapter repeats preparation in the parent renderer; its
retained evidence remains labelled that way. The current fixture generates a
temporary renderer from the retained TypeScript owner, asserts exactly one source
preparation seam, and substitutes the worker's prepared source plus typed math
fragments at that seam. Relative imports resolve to their actual owners. No
production file is modified; sanitation, code, math and DOM projection still use
the production implementations. The assertion fails if the production seam changes.

The revised CPU interval excludes the full semantic oracle, which runs afterwards
against every captured frame. CPU still includes startup, 20 warmups, timer probes,
DOM HTML capture and result aggregation; short workloads hit browser clock/CPU
resolution floors. This is fixture CPU, not a clean production CPU/op estimate.
Memory runs are independent; renderer RSS is sampled every 5 ms during the loop,
not during the oracle. Samples include captured frame strings; a sampled maximum
can miss shorter spikes. The timer probe measures event-loop servicing, not actual
input-to-paint or scrolling. Worker startup has only three observations per case.
Worker crashes reject pending work and permanently close later admission.

Neither experiment supplies application reader interaction, actual CSP/deployment
or incremental parsing coverage. Whole-system Markdown admission remains incomplete.

`inline_cache.js` is a separate synchronous owner-scoped inline-lexer reuse
experiment. Each parse still performs canonical full block lexing and rendering.
The cache key includes complete reference definitions and entry lexer state;
hits restore detached JSON tokens and the original resulting state. Canonical
task-list rendering can mutate tokens, so shared token objects are never reused.
Non-JSON/custom token values bypass cache admission. An explicit policy epoch
clears earlier entries, a byte budget bounds the LRU, and disposal is terminal.
This fixture supports the current production strict-delete policy, not arbitrary
Marked preprocess/postprocess/walk-token extensions or asynchronous parsing.

Run the compatible Node binary with `benchmarks/markdown_worker/check_inline_cache.mjs`.
It bundles the actual policy source and checks growing prefixes, late/changed
references, raw-block/link states, task lists, Unicode/CRLF, seeded combinations,
owner denial, policy changes, byte eviction and terminal disposal. The same
production renderer/DOMPurify, actual browser latency/RSS, input/paint, CSP and
reader interaction remain required before adoption; no performance gain is claimed.
**Problem:** full reparsing repeats inline work on unchanged paragraphs.
**Decision:** reuse inline derivations while retaining full block discovery and
global-reference dependency in the key. **Alternatives considered:** assuming
an appended block cannot change an older reference loses correctness; sharing
mutable tokens contaminates task-list rendering; a background worker already
regressed latency/RSS. This is partial reuse, not a fully incremental parser.

`tests/visual/test_visual_markdown_inline_cache.py` verifies the candidate through
the unmodified production renderer, DOMPurify and forced layout at an owned local
HTTP origin. It checks growing fragments, terminal/streaming renders, escaped/raw
HTML, unsafe links, math/code/tables, policy changes and cache reclamation.
`TOFU_MARKDOWN_INLINE_CHECK_OUTPUT=/tmp/inline-browser-check.json` retains the
browser version, actual comparison count, checks and source digests. This is
browser semantic/lifecycle evidence only; latency, CPU, RSS, input-to-paint and
full application CSP/selection/scroll remain unmeasured.

`browser_measure.py` supplies the opt-in
`test_inline_cache_paired_browser_measurement` comparison. Set
`TOFU_MARKDOWN_INLINE_MEASURE_OUTPUT=/tmp/inline-browser-measure.json` and run
that visual test for five workloads, three alternating independent browser pairs,
100 observations after 20 warmups and separate renderer-RSS browsers. Every
latency frame is checked against canonical production DOM after measurement.
Memory runs retain no oracle frame strings. A separate reset/rewarmed interval
uses fresh source versions for CPU, excludes timer/oracle/RSS work and verifies
its final DOM. Its CPU cohort mean is not a per-operation raw CPU vector and
cannot complete the strict system CPU gate. The two-observation prose smoke
checks fresh browser identities and sampling mechanics only. Full cold startup,
input-to-paint, reader interactions and application CSP remain coverage gaps.
The full inline-cache run verifies 3,000 latency samples in 60 independent
browser processes and every captured production DOM frame. Prose P50 improves
about 19%, but renderer RSS rises 37%; reference workloads regress latency/CPU
and several P99 guards fail. The current candidate is unpromoted; full
[inline rejection evidence](../../docs/evidence/system_markdown_inline_20261008.json)
retains per-pair ratios, source/artifact identities and CPU/coverage limitations.

`reader_measure.py` and `reader_measure.js` compare current reader reconciliation
against both frozen text/stable-DOM owners from `5c55a9d1b`. Both variants use
current production Markdown policy/renderer, installed Marked/DOMPurify and real
highlight.js. Mixed paragraph/code/table tails and stream-to-terminal highlighting
use 160 stable rich paragraphs, forced layout, 20 warmups, 100 timed observations
and three alternating independent browser pairs; RSS uses separate browsers.
Every captured latency frame and the final CPU frame match canonical full DOM.
Source and compiled dependency-graph digests fence the experiment.
Run `tests/visual/test_visual_reader_measurement.py`; set
`TOFU_READER_PERF_OUTPUT=/tmp/reader-measure.json` for the full comparison.
The smoke checks measurement mechanics only. CPU is a separate transition-cohort
mean; completion CPU includes preparatory streaming while its latency times only
the terminal render. No raw CPU/op, retained memory, cold startup, actual input
paint, full-app or CSP admission follows from these results.

The full reader comparison validates 1,200 timed observations in 24 independent
latency/RSS browsers. Mixed-tail P50 falls 46–69%; completion P50 falls 79–80%.
The first-pair renderer RSS rises 11.7%/16.1%, exceeding the 1.05 guard; later
near-neutral pairs cannot erase those failures. The correctness fix is integrated,
but measured performance admission remains false.
[Retained reader evidence](../../docs/evidence/system_markdown_reader_20261008.json)
records all pairs, raw/bundle identities and concurrent-host/CPU/coverage limits.

`reader_memory_profile.py` privately reuses the frozen-owner reader comparison
with explicit phase observations: loaded, warm, latency, CPU warm/loop, oracle,
disposed and disposed-after-GC. Real CDP heap/DOM counters and Linux renderer
RSS/VmHWM diagnose allocation/retention separately. The memory workload retains
no frame captures. It deletes the owned fixture after disposal before collecting
garbage; GC never runs inside a timed or active-reader operation. Instrumentation
perturbs the workload, so these results cannot replace the original RSS failures
or supply performance admission. Run `tests/visual/test_visual_reader_memory_profile.py`;
set `TOFU_READER_MEMORY_PROFILE_OUTPUT=/tmp/reader-memory-profile.json` for the
full 100-observation/three-pair diagnosis. Concurrent-host activity remains declared.

The full phase diagnosis uses 12 independent browser processes with 100-operation
latency/CPU cohorts, three pairs per case. Post-disposal/GC JS heaps are similar
(about 3.1–3.3 MiB), while active DOM counters exceed 100,000 before dropping.
This points to transient DOM allocation pressure in this fixture; one lifetime
does not prove long-run retention or remove the original RSS guard failures.
[Retained phase evidence](../../docs/evidence/system_markdown_reader_memory_profile_20261008.json)
keeps all observed phases and instrumentation/concurrency limits.

`inplace_sanitizer.js` tests owner-scoped DOMPurify IN_PLACE adapters. The
fragment/div variant is rejected by a full-document head/style counterexample.
The document variant reuses one inert HTML document, sanitizes its body with
the installed DOMPurify, matches its leading-whitespace rule, and clears both
head/body after every call. Default installed config only; no global installation.
`tests/visual/test_visual_inplace_sanitizer_experiment.py` compares hostile/raw/
escaped/partial Markdown through the actual renderer, pins fragment rejection,
and checks cleanup/disposal. Current corpus equality is not broader security,
CSP, Trusted Types, hook/config or performance admission. `inplace_measure.py`
reuses the owned independent-browser driver for five workloads and resets the
oracle parser AND sanitizer to canonical dependencies. Set
`TOFU_INPLACE_SANITIZER_PERF_OUTPUT=/tmp/inplace-measure.json` for full pairs.
CPU remains a cohort mean; no raw CPU/op or whole-app admission follows.

The full in-place document trial verifies 3,000 canonical DOM frames across 60
independent latency/RSS browsers. Prose P50 regresses 11–15%, references 22–27%,
and terminal 40–50%; CPU and several RSS guards also fail. The candidate remains
unpromoted. [Retained rejection evidence](../../docs/evidence/system_markdown_inplace_sanitation_20261008.json)
keeps all pairs, dependency/artifact identities and resolution/coverage limits.

`tests/visual/test_visual_streaming_markdown_security.py` serves actual renderer
owners and installed Marked/DOMPurify as same-origin scripts under a strict
fixture CSP. An inline-handler control must be blocked. The browser runner
checks raw/partial/code/table HTML and SVG/MathML attack inputs against complete
authorized DOM on every stream/terminal frame, including unchanged prefix node
identity and dangerous attribute/URI/element absence. Results are delivered
through DOM text, avoiding CSP-forbidden string evaluation by the test driver.
[Streaming security evidence](../../docs/evidence/system_markdown_stream_security_20261008.json)
records 84 streams/918 frames and actual CSP/dependency/bundle identities.
Adjacent reader/streaming DOM tests pass (full performance is opt-in). This
fixture does not deploy CSP to the main app, prove Trusted Types/custom hooks,
or establish CSS safety without CSP; math/highlighting, backend and performance
remain separate gates. Existing latency/RSS admission failures are not waived.

`reader_input.py`/`reader_input.js` exercise real keyboard events with controlled
synchronous production rendering and forced layout. Each key is followed by two
animation frames; every resulting DOM is checked against canonical rendering,
and all typed characters must survive. The reported keydown-to-second-frame
interval is a scheduling proxy, not physical presentation or INP. Completion
preparation is included. Run the owning visual smoke in
`tests/visual/test_visual_reader_input_contention.py`; full paired runs require
`TOFU_READER_INPUT_OUTPUT`. This fixture does not cover actual application
scheduling, peak/retained memory, raw per-key CPU, or whole-domain admission.

Problem: cohort-average CPU and two-case reader results omit the five declared
workloads and retained-residency checks. Decision: `domain_reader.py` measures
actual parser/sanitizer/highlighter/renderer and forced layout in independent
browsers for prose, open code, tables/references, prefix selection/scroll and
terminal rendering (including preparation). Every frame is checked against
canonical full rendering. Alternatives rejected: parser-only CPU omits layout;
overlapped process intervals double-count CPU; UI snapshots cannot prove native
selection/scroll preservation. RPC dispatch is explicitly included in sampled
browser/renderer CPU; wall measures the renderer's operation.

`tests/visual/test_visual_markdown_domain.py` smoke owns twenty independent
latency/memory browsers. Full runs require `TOFU_MARKDOWN_DOMAIN_OUTPUT` and
use 100 samples/three pairs per case. The local HTTP fixture loads actual built
styles and retained renderer owners with installed Marked/DOMPurify/highlighting.
Reference reconciliation is frozen at the existing `5c55a9d1b` witness. Memory
browsers collect before the warmed baseline and after renderer disposal,
sample renderer RSS at 5ms and sum renderer VmHWM for a conservative peak.
Multiple-renderer high-water sums may exceed simultaneous peak; one disposal
interval is not long-run leak proof. Sources, compiled graphs and dependencies
are frozen. API/full-app, physical paint, math and deployment-security remain
separate gates. Run streaming DOM/security neighbors; do not waive their errors.
