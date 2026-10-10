# Markdown worker experiment guidance

This directory owns a browser-only parser RPC fixture, not application rendering
or conversation state. `client.js` bounds work to one running and one newest
waiting request, rejects stale completions, and terminates explicitly.

Pure preparation and policy use bundled retained frontend owners. Production
rendering/sanitization remain on the main thread in the visual test. Read README.md
for measurement limitations; do not promote this adapter from diagnostic CPU/RSS.
Verification is `tests/visual/test_visual_markdown_worker_experiment.py` served
from its owned local HTTP origin. Generated bundles/results remain outside Git.

`inline_cache.js` owns a separate owner-scoped synchronous lexer-reuse fixture.
`check_inline_cache.mjs` checks installed Marked plus the production policy in
Node. It retains full block lexing and does not establish browser performance;
production rendering/sanitation and visual verification remain separate owners.

`reader_measure.py`/`reader_measure.js` compare frozen prior reconciliation owners
with current production readers and real highlighting. Verify measurement/oracle
mechanics through `tests/visual/test_visual_reader_measurement.py`; full paired
runs are opt-in. Keep completion preparation outside timed terminal latency and
declare its inclusion in CPU cohorts; never turn cohort means into raw CPU/op.

`reader_memory_profile.py` diagnoses phase RSS/high-water, JS heap and DOM counts
via a private clone of the reader fixture. Verify its owning visual smoke before
full pairs. GC belongs only after fixture disposal; instrumented results never
replace the original performance failures or enter admission.

`inplace_sanitizer.js` owns detached fragment/document sanitation experiments.
The fragment is intentionally rejected; the document variant is exact-oracle
gated and empties its scratch after each call. Verify its visual corpus and
`inplace_measure.py` smoke before full pairs. Canonical oracle must reset both
parser and sanitizer; keep default-config and security/deployment gaps explicit.

`reader_input.py`/`reader_input.js` exercise real keyboard events with controlled
synchronous production rendering and forced layout. Each key is followed by two
animation frames; every resulting DOM is checked against canonical rendering,
and all typed characters must survive. The reported keydown-to-second-frame
interval is a scheduling proxy, not physical presentation or INP. Completion
preparation is included. Run the owning visual smoke in
`tests/visual/test_visual_reader_input_contention.py`; full paired runs require
`TOFU_READER_INPUT_OUTPUT`. This fixture does not cover actual application
scheduling, peak/retained memory, raw per-key CPU, or whole-domain admission.

`domain_reader.py`/`domain_reader.js` own five reader-domain workloads, exact
per-frame DOM and native prefix selection/scroll checks. Verify the visual
smoke and streaming/security neighbors before full pairs. README.md defines
RPC CPU, renderer RSS/high-water, GC/disposal and full-app/paint exclusions.
