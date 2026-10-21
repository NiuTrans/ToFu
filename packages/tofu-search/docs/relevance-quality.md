# Search evidence quality

## HTML source fidelity

**Problem:** the previously allowed Trafilatura 2.0 installation moved nested
link/code labels after their surrounding words, removed code indentation and
allowed one extraction's deduplication state to affect later calls. Version 2.2
still corrupted bracketed code identifiers and lost some short code blocks.
These are evidence defects even when retrieval succeeds.

**Decision:** require Trafilatura 2.3.1 or newer. Use the community's
[nested-element fixes](https://github.com/adbar/trafilatura/pull/872) and
[subsequent extraction corrections](https://github.com/adbar/trafilatura/releases/tag/v2.3.1)
through the existing serialized extractor. Network policies, source limits,
link inventories and the BeautifulSoup fallback retain their current owners.
`tests/test_html_fidelity.py` exercises the actual dependency, including repeated
calls, rather than mocking its output. Published package metadata and the
workspace lockfile must both exclude the affected versions.

The boolean `deduplicate=True` still selects Trafilatura's process-global cache.
Upgrading alone does not isolate repeated document reads: after several calls,
valid source paragraphs and short values can disappear or trigger a different
fallback. Pass a fresh `LRUCache` through the documented extraction parameter
for each document, retaining the upstream capacity. The cache lives only inside
the already serialized extraction call; it creates no resident cache or worker.
Repeated-call specifications exceed the upstream repetition threshold.

Adjacent terminal `div` values can disappear when the surrounding page has
longer prose. Before article extraction, group consecutive inline-only cells
into a paragraph with explicit line breaks, keeping their source order,
attributes, inline markup and following text. Require at least two cells with
words outside links so linked recommendations keep their existing extraction
heuristics. Do not rewrite block-bearing containers, navigation elements,
tables or preformatted content, or infer CSS columns and label/value mappings.
The native tree is call-local and remains under the existing extraction lock.

Both extraction paths exclude explicit `hidden`, `aria-hidden="true"` and
inline `display:none` elements while retaining visible following text. This
does not evaluate stylesheets or JavaScript. `tests/test_html_value_blocks.py`
covers short multilingual fields, mixed inline links, hidden content, nearby
prose, related cards and source order through the real dependency.

Article heuristics can still flatten list-nested or highlighted code and remove
blank lines. The existing supplemental-structure pass therefore compares
preformatted DOM text literally, preserves indentation and internal newlines,
and emits a sufficiently long Markdown fence when the original block is absent.
Repeated blocks are added once. Hidden and excluded elements remain outside
that sanitized content boundary; source and presentation limits still apply.
The supplement may be omitted by a small final prefix budget, so an uncapped
read or query-focused excerpt can be needed for code late in a long page.

**Alternatives rejected:** flattening all markup loses source structure;
reconstructing sentences with an LLM invents evidence; copying upstream parser
internals creates a second maintenance owner. Converting every terminal `div`
into a paragraph can displace article bodies. Converting a selected DOM subtree
directly to Markdown retains more navigation; disabling deduplication also
changes upstream article selection. Neither replacement improved the frozen
[Mozilla Readability fixture](https://github.com/mozilla/readability/tree/ab4027a8b37669745016869a37a504727992b2ba/test/test-pages)
comparison consistently. Those reference pages primarily test article parsing,
not field completeness or agent answers. Passing the new tests proves
these extraction behaviors, not general retrieval superiority or that an agent
will quote the evidence correctly.

## Received HTML streaming completions

**Problem:** React can send readable HTML inside a hidden transport container
after its initial shell. Removing all hidden elements immediately loses that
received body and leaves navigation or an obsolete loading fallback.

**Decision:** `fetch/html_streaming.py` restores recognized Fizz completion
declarations before the shared extraction and fragment-selection boundary.
The behavior follows React's
[streaming contract](https://react.dev/reference/react-dom/server/renderToPipeableStream#streaming-more-content-as-it-loads)
and [boundary replacement algorithm](https://github.com/facebook/react/blob/v19.2.0/packages/react-dom-bindings/src/server/fizz-instruction-set/ReactDOMFizzInstructionSetShared.js).
Only an inline classic script containing literal, direct `$RC` instructions
(optionally preceded by the Fizz `$RB`/`$RV`/`$RC` declarations) is recognized.
The declaration scanner skips function bodies without evaluating them; it is
a protocol recognizer, not an interpreter or proof that arbitrary page scripts
would execute successfully. Unknown statements reject that script's instructions.

Require unique identifiers/instructions, matching identifier prefixes, an empty
boundary template between paired React comments, and an already received
`div` with only its `hidden` and `id` transport attributes. Restore children in
the boundary's original position, replace the old fallback, and retain nested
completion order. Destination and child visibility rules remain in force.
Ambiguous or malformed pairs, inert/JSON/module scripts, deferred or conditional
calls, unsupported runtime syntax and unknown segment forms remain unchanged.
Serialized application data and incomplete client-rendered bodies are not
invented into visible source text. This does not evaluate CSP or stylesheets.

Work remains within the existing bounded response and request lifetime, with
no extra network/model call, dependency, worker, persistent state or browser
requirement. Ordinary HTML without stream markers avoids the extra parser;
unrecognized candidates return their original HTML. Stream pages pay for a
call-local DOM and serialization. Same-parent comment pairs are indexed once,
so missing closing markers do not cause repeated whole-page scans.

An offline replay of a captured 816,567-byte page recovered 1,514 characters
from a previous 179-character navigation-only output. Three alternating serial
samples gave median extraction times of 75.6 → 115.5 ms; the normalization pass
alone took 53.0 ms with 7.8 MB peak traced Python allocations in a separate run.
A different page with no server-rendered biography remained without that body.
Short-readable and documentation-fragment controls retained identical text.
Synthetic pages with 200/800/1,600 independent completions took 26/184/605 ms:
DOM mutation cost grows with segment count, so these figures do not establish
linear performance or an end-to-end deadline guarantee. These shared-host
measurements describe development inputs, not general speed or answer quality.

**Alternatives considered:** including all hidden nodes exposes unrelated
content; parsing arbitrary application JSON invents a second rendering owner;
requiring a browser spends more resources even when the HTML is already here.
Raising SPA text thresholds misclassifies genuine short sources and does not
recover received HTML. Keep those concerns with their existing owners.
`tests/test_html_streaming.py` covers source order, nested/incomplete boundaries,
visibility, fragment/code/table fidelity, runtime bootstrap syntax, ordinary
fast paths and public fetching without an extra request. A reviewed React
runtime fixture is retained under `tests/fixtures/` with its MIT license.

## HTML fragment scope

**Problem:** reading a documentation URL with an observed `#fragment` returned
the same whole-page prefix as its bare URL. A definition late in a large manual
could remain outside the output budget even after an agent followed its anchor.

**Decision:** `fetch/html_fragments.py` selects a literal identifier from the
existing sanitized DOM, decoding a conventional fragment exactly once. Heading
scopes include subordinate headings; definition scopes retain aliases and
their descriptions; disclosure scopes retain both the summary signature and
the associated body. The common extractor preserves source wording, code,
tables and links. Duplicate, absent, hidden, unsupported or unreadable targets
keep the original whole-document extraction. Text-fragment directives are not
interpreted. Code-hosting extraction keeps its existing priority.

HTTP fetching accepts a successfully extracted short scope without treating it
as an empty page or SPA shell. The existing SPA check uses sanitized whole-page
text, so a matched loading stub still needs rendering. Bot/login checks,
request admission, deadlines,
byte limits and final output bounds remain in force. Cache keys retain the
requested fragment, so a scoped result cannot replace a whole-page result.
Plain-text readers and browser body-text responses may return the whole page;
the selector does not invent markup for those paths. Removing the fragment
requests the complete document through its existing output limits.

Selection is call-local and adds no request, model, dependency, resident worker,
cache or durable state. It parses at most the already byte-bounded HTML; an
unresolved fragment pays the additional DOM pass before normal extraction.
In an identical-source replay, a 43,777-byte Python reference page shrank from
10,314 to 309 extracted characters; a 955,558-byte Rust reference page shrank
from 300,940 to 2,106. Both target definitions remained readable within a
20,000-character output budget. Warm serial extraction medians, four samples
per arm with alternating order, were 56.1 → 33.8 ms and 1,305.5 → 554.7 ms.
For missing anchors on those same pages, whole-page output stayed identical
but medians increased from 56.2 to 82.8 ms and from 1,340.8 to 1,880.7 ms;
the extra DOM pass is a measurable fallback cost. Bare URLs avoid that pass.
These two development pages establish scope and local extraction cost, not
network speed, broad accuracy or correct agent quotation.

**Alternatives considered:** slicing at a matching token can split a definition
and confuses prose with anchor identity. Selecting only the target element
loses Sphinx descriptions and Rustdoc bodies located beside signatures. An LLM
summary changes evidence and introduces cost. Structural selection reuses the
existing fidelity boundary and fails back to the full document. Specifications
live in `tests/test_html_fragments.py`; actual MCP calls exercise the same
public fetch path in `tests/test_mcp_tool_calls.py`.

## PDF failure evidence

**Problem:** PDF extraction returned human-readable failure placeholders as
nonempty strings. Fetching could cache them as document bodies, and callers
could mistake a missing optional dependency or parse error for source evidence.

**Decision:** `extract_pdf_text` raises `PDFExtractionError` for unavailable
capabilities, oversized inputs, parse failures and documents with no extractable
text. `fetch_page_content` returns `None` with `diag.reason=pdf_extraction_error`
and an actionable detail; failures never enter its body cache. Empty rich
extraction first tries the existing raw-text fallback. Optional dependencies
still load lazily, document handles close, and successful source text is kept.

**Alternatives considered:** matching error-shaped text at each caller would
misclassify documents that quote those same messages and leave other callers
exposed. Installing native PDF dependencies for every web search would add
startup and deployment costs. The extractor owns typed failure; fetch owns its
existing failure/diagnostic projection.

## Snippet groups and fetched evidence

**Problem:** similar titles and search snippets need not describe identical
pages. Discarding a listing before examining an already received body loses
evidence and hides discovered source URLs. Removing snippet grouping entirely
also changes the representative ranking, so it requires separate quality
evidence rather than assuming that more flat results are better.

**Decision:** retain the existing first representative and Jaccard grouping for
snippet-only ranking. Keep the other observed listings in request-owned groups.
Already completed alternative fetches enter the existing body deduplication,
relevance and ranking stages; an early stop consumes settled receipts once
without waiting on running work. Fetch admission, worker ownership and deadlines
remain with their existing owners. Optional model filtering can consequently
process more distinct received bodies within its existing budget.

Returned rows may contain `similar_snippets` with exact URLs, titles, snippets
and available listing provenance. One allowance of `max_results` identities is
shared across primary rows and the existing three-entry uncertainty preview,
allocated one per group per round. `similar_snippets_omitted` reports additional
listings left out by that allowance. Provider-injected nested candidates,
site-excluded pages, model-rejected bodies and their known copies cannot enter
this projection. Existing visible primary and body-copy URLs are not repeated.
The groups imply neither verified page equivalence nor independent corroboration;
they do not merge engine votes or create fetch authority for unseen URLs.

The common library/host/MCP formatter gives primary evidence its existing
allocation first. Optional related snippets use only the remaining shared
content budget. Exact source identities remain visible when that budget is
exhausted, and the host's canonical capture retains the original records.
This adds no service, request, resident pool, cache or durable state. Temporary
groups refer to already collected rows; returned metadata is bounded by the
shared identity allowance rather than multiplying per primary result.

**Measurement:** a development replay of 149 previously inspected free-search
pools retained identical primary projections and primary excerpts. It exposed
324 additional observed identities in 109 cases, at a median 852 additional
formatted characters (maximum 4,282) with an 18,000-character payload budget.
Sequential local pipeline medians were 17.98 ms before and 17.66 ms after;
these include worker setup and establish no network-latency advantage. Seven
small pools were reconstructed from complete ranking/uncertainty snapshots.
These are compatibility and output-cost measurements, not fresh holdout,
equal-identity-budget retrieval gains, or answer-accuracy scores.

**Alternatives considered:** lossy snippet removal hides sources; unconditional
flat expansion can displace better representatives; treating snippet overlap
as body duplication invents equivalence. The representative-plus-members
pattern is also used by [Elastic result collapsing and inner hits](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/collapse-search-results),
but this implementation groups lexical overlap and exposes already observed
rows without expansion queries or an Elastic dependency. Behavioral specs live
in `tests/test_similar_snippet_candidates.py`, actual MCP calls in
`tests/test_mcp_tool_calls.py`, and the host capture contract in
`tests/search/test_search_similar_candidates.py` at the repository root.

## Uncertain discovery candidates

**Problem:** selecting the first three uncertain listings exposes engine
arrival order instead of the most promising follow-ups. This can hide a useful
source behind generic index pages, especially when a mixed-language query has
only partial lexical overlap with a document.

**Decision:** pass uncertain candidates through the existing BM25/rank-fusion
and diversity owner before selecting the diagnostic preview. Keep the complete
received sidecar; only its ordering changes. Primary evidence is ranked in its
own pool. Neither ordering nor a successful fetch upgrades lexical uncertainty
to factual support. Explicit `rerank=False` keeps both pools in their existing
order. The library, host and MCP share this projection and its existing bounds.

No network/model call, resident worker, cache or durable record is added. The
extra local scoring pass is bounded by the already received candidate pool.
In a development replay of 152 observed pools, primary evidence and the full
uncertain inventory were identical. Sequential local pipeline medians were
18.44 ms before and 18.54 ms after, including worker setup. The formatted-size
delta had a zero median and a 1,282-character maximum; selected identities can
have different lengths even though the count and payload budgets are unchanged.
These measurements do not establish answer accuracy or general search quality.

**Alternatives considered:** increasing the preview size consumes more agent
context without selecting better follow-ups; weakening the relevance threshold
would overstate partial or cross-language evidence; adding a second uncertainty
scorer duplicates ranking policy. Behavioral specifications cover partial
matches, mixed scripts, received bodies, primary separation and the explicit
opt-out in `tests/test_uncertain_candidate_ranking.py`, with native host and MCP
invocation tests at their existing boundaries.

## Ranking and excerpts

**Problem:** a collection description can repeat every word of a question while
adding only instructions to browse posts or watch reels. Complete lexical overlap
then misrepresents the metadata as useful subject evidence.

**Decision:** `search/quality.py` treats a narrow, explicit English collection
template as uncertain when both title and snippet cover the complete query
subject and their remaining words supply only a collection action, media kind
and boilerplate. At least three query terms are required. The novelty check
keeps raw normalized words, including negations and spelled-out numbers; any
additional name, value, qualification or unsupported wording keeps the existing
classification. Source text is never rewritten. `sources.serp.query_echo_only`
explains the uncertainty, and the URL remains available for follow-up. Fetched
body evidence is evaluated separately and can establish relevance later.

**Alternatives rejected:** blocking a host would also suppress genuine articles;
requiring every search snippet to answer the question would discard useful
discovery leads. Rank votes cannot establish that query-only metadata contains
useful evidence. The query-only check owns that observation boundary, with no
model, external service or new worker. It is not a general spam or truth detector.

**Problem:** snippet BM25 scores depend on metadata wording and the candidate
pool. Adding a small fixed multiple of engine rank can let keyword-heavy
metadata overwhelm useful upstream placement even after relevance admission.

**Decision:** for pools with no fetched body and at least one upstream rank,
combine the existing upstream reciprocal-rank sum with one local lexical rank
vote, `1 / (60 + rank)`. The collector and local ranker share the same offset,
following [reciprocal rank fusion](https://www.elastic.co/docs/reference/elasticsearch/rest-apis/reciprocal-rank-fusion).
Local ranks retain the current lexical scorer and bounded source hints. Only
positive lexical evidence admits rank votes; engine counts do not add a second
consensus bonus. Equal fused scores prefer the local lexical rank, then original
position. Existing entity and domain diversity apply afterward. Pools with any
fetched body, no upstream ranks or no query subject retain their current policy.

The same owner ranks snippets before fetching, so this can change which URLs
receive the existing fetch budget. It adds no service, worker, model call or
budget. `tests/test_snippet_rank_fusion.py` and the public small-pool tests cover
upstream placement, query-only uncertainty, zero-evidence candidates, fetched
bodies and explicit `rerank=False`.

**Alternatives rejected:** a raw-score multiplier needs calibration across
different queries and metadata lengths; engine-only ordering discards local
evidence. Equal rank sums with arrival-order ties can restore a weaker first
result when the upstream and lexical orders oppose each other. Lexical tie
ordering preserves the stronger match in that case. Engine agreement remains
a retrieval signal, not independent verification or an answer-quality grade.

**Problem:** a fixed document prefix can hide the answer even after a successful
fetch. Search operators, navigation inventories and repeated text can consume
ranking weight or the model's evidence budget. Returning a small candidate
pool without sorting also exposes engine arrival order as apparent relevance.

**Decision:** `tofu_search/lexical.py` owns Unicode matching and query-subject
parsing for fetch admission, ranking and excerpts. Retrieval operators and
negative operands are constraints, not positive subject evidence. Quoted
literals remain subjects. CJK bigrams come from contiguous runs; non-Latin
words, accents and numeric versions remain matchable. This is lexical matching,
not semantic equivalence or factual verification.

Technical symbols are lexical evidence: `C++` and `C#` remain distinct,
and balanced `O(...)`, `Ω(...)` and `Θ(...)` notation retains a bounded atomic
feature. Spaces around operators normalize only for matching; spaces between
names remain significant (`n log n` differs from `nlogn`). Ordinary words and
numeric versions remain available alongside the added features. Quoted-subject
checks use the same boundaries, including stop words and negations. Source
excerpts and outbound queries are unchanged. Recognition examines at most 128
characters from each opening parenthesis; malformed or longer notation keeps
ordinary word tokens. This is not a programming-language or math parser.

**Problem:** stripping punctuation made some technical queries empty or tied
different languages/complexities, hiding late evidence. **Alternatives rejected:**
rewriting search queries changes provider semantics; interpreting formulas or
using a model adds an independent matching authority. Shared deterministic
features fix ranking, admission and excerpt selection together. Text without
candidate symbol syntax uses the ordinary word scan directly.

`passages.py` scans the complete supplied body, excluding the extractor's
explicit link inventory. It selects source spans using lexical coverage,
phrase matches, bounded term density and complementary query terms. Repeated
text does not consume another excerpt slot; similar passages are deprioritized,
but different values and qualifications remain eligible. Gaps are marked;
overlapping spans are merged rather than repeated. Character limits include
gap markers. An optional lead uses at most one third of the budget so page
identity and relevant body evidence can both reach a relevance filter.

`search/rerank.py` builds its bounded BM25 representation from those passages
and SERP metadata. The representation limit is not a source-prefix limit.
The public search entry point ranks candidates even when the pool fits within
the requested count; only explicit `rerank=False` disables this step. Skipping
ranking when no truncation is needed leaves the earliest engine result first,
even when a later result covers more of the query. Public-entry regression tests
exercise both snippets and fetched bodies, alongside the ranking helper tests.
URL source hints, engine-agreement and diversity signals remain.
Original result dictionaries and bodies are preserved for fetch follow-up.

**Alternatives considered:** increasing the prefix limit moves the same failure
to a longer document and dilutes relevance with more boilerplate. An LLM rewrite
can change source wording and adds model latency. Selecting independently
ranked chunks without a coverage/diversity pass repeatedly spends context on
the same aspect. Optional neural reranking remains a separate capability to
evaluate; these deterministic improvements require no model or paid service.

The algorithm allocates only call-local structures and creates no workers,
caches or persistent data. Chunk scoring scans the body, while heap selection
updates priorities only when chosen evidence changes. It does not repeatedly
sort every remaining candidate when none fits the remaining budget.

## Focused page reads

Problem: a bounded page read exposes a prefix even when the question's evidence
appears later. The host's separate extraction-window matcher also dropped short
CJK and symbolic technical terms already supported by the shared lexical owner.

Decision: `fetch_page_content(..., query=...)`, the `fetch_url` facade and MCP
`fetch_page` use the existing passage selector over the complete received
source, preserving exact wording and marking gaps. Empty queries retain the
ordinary pipeline. Explicit or configured `max_chars=0` retains all received
text. A focused projection sets `diag.truncated` when it omits source text;
failure diagnostics remain owned by the raw fetch. Selection never establishes
absence, relevance or entailment by itself.

Focused reads use the existing full-source path and bypass the preview cache:
an old truncated entry cannot prove source completeness. Selected text does
not overwrite raw cache entries. The existing byte, extraction, PDF and
deadline limits still apply; one call does not launch a second fetch or model
pass. This trades possible warm-cache latency for evidence coverage. There is
no new persistent store, worker pool or optional dependency. Literal URL
fragments retain their existing scope before query selection.

The host's `lib/search/search_extract.py` uses that same selector for its
48,000-character evidence budget and existing single model pass. It retains
page identity and uses the selector's explicit `fallback_edges` policy to keep
the beginning and end on a lexical miss. Quote validation, raw source artifacts,
model admission, cancellation and failure fallback retain their existing owners.

Measurement: six fixed late-evidence questions over captured Python and Rust
manuals retained their exact reference quotes in 6/6 focused outputs versus 0/6
prefix outputs. Both requested 2,000 characters; focused output including gaps
stayed within that cap, while the baseline appended its 13-character truncation
marker. All selected spans remained literal source text. With four uncached
samples per case, Python medians were 57–61 ms before and 60–65 ms after; Rust
medians were 1.30–1.33 s before and 1.38–1.42 s after. The arms ran concurrently
on a shared host, so these timings are diagnostic, not an isolated speed claim.
Python-managed allocation peaks remained about 1.34 MB and 31.6 MB respectively;
process lifetime RSS peaks, including imports, were 151 and 152 MiB. Tracemalloc
does not cover all native DOM allocations. This selected-source replay uses no
live network or model and measures coverage and local cost, not answer accuracy.

Alternatives rejected: increasing every preview budget, another matcher in the
MCP adapter, rewriting source text, trusting a preview cache as complete, or
weakening quote checks to improve a diagnostic score. The design is informed
by [MCP Fetch's continuation interface](https://github.com/modelcontextprotocol/servers/tree/main/src/fetch)
and [Exa's separate full-text/highlight views](https://github.com/exa-labs/agent-skills/blob/main/skills/build-with-exa/references/contents.md);
neither service is a dependency or a quality baseline for this implementation.

Verify `tests/test_query_fetch.py`, `tests/test_ranked_passages.py`, real MCP
calls, and the host's `tests/tasks_pkg/test_fetch_url_full_source.py`. Captured
source comparisons must keep byte-identical inputs and output budgets; synthetic
regressions and selected pages do not establish search leadership.

## Source hints and relevance

**Problem:** a hostname suffix collision could inherit a reference-host hint,
and a query word in an arbitrary subdomain was presented as verified official
identity. Absolute URL bonuses could put a page with no query evidence above
an answer. Engine agreement and entity coverage could repeat that mistake.

**Decision:** `search/authority.py` matches normalized HTTP(S) DNS hostnames at
label boundaries. Query/host spelling, reference/documentation host patterns,
and aggregator-like names are explicitly **unverified hints**. They do not
certify ownership, primary-source status, or factual accuracy, including on
shared repositories, registries, documentation platforms, and `.edu` hosts.
The existing `Authority:` output field retains this qualification per result,
including in the host's compact persisted index.

Source hints retain their existing adjustment ceilings, but a positive hint
cannot contribute more than the lexical score itself, and a negative hint
cannot remove more than half of it. Engine agreement and successful extraction
also require query evidence; results with no positive lexical score cannot
reserve an entity or domain-diversity slot. They remain available when filling
remaining result slots rather than being deleted.

**Alternatives rejected:** hostname spelling cannot establish publisher
identity, even with better suffix parsing. A larger absolute bonus still lets
a URL replace missing evidence. Replacing the ceilings with small fractional
weights demoted useful documentation in recorded candidates; removing all
source hints also discards useful tie-breaking information. These conservative
hints require no remote verification service, model, new cache, or additional
network requests.

`tests/test_source_hint_boundaries.py` specifies suffix collisions, URL
normalization, unverified labels, lexical relevance, and coverage behavior.
The host persistence test carries the qualifier through disk offload.

## Verification and measurement

### Engine availability is separate from relevance

**Problem:** successful HTTP status alone does not prove that an engine
completed a search. A small challenge page can be shorter than a size heuristic,
and repeated HTTP 202 responses can otherwise look like successful empty results.
**Decision:** the shared HTTP envelope treats outstanding 202 responses, the
documented Cloudflare challenge header, and DDG's challenge form as unavailable
coverage. Existing bounded retries and already-configured alternate network
paths still apply. Exhaustion remains an engine error; no new engine, browser
authority or credential is selected. Real empty responses and search results
discussing CAPTCHA remain valid.
**Alternatives rejected:** relying on response size misses small interstitials;
matching CAPTCHA-related prose rejects legitimate topics; accepting an empty
challenge resets failure learning and prevents recovery.

The structural indicators follow
[Cloudflare's response contract](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
and the [SearXNG DDG adapter](https://github.com/searxng/searxng/blob/master/searx/engines/duckduckgo.py).
`tests/test_engine_block_pages.py` covers exhaustion, genuine recovery, ordinary
no-match pages, topic false positives and configured path failover.

### Offline measurements

The package's offline specifications cover late answers, small candidate pools,
constraints, link inventories, multilingual text, complementary facts, page
identity, original-text fidelity and hard output budgets:

```bash
uv run --package tofu-search pytest packages/tofu-search/tests
```

The repository benchmark keeps manually labeled synthetic cases separate from
production scoring. It reports hit@1, reciprocal rank, graded nDCG at each case's
requested count, literal fact recall from delivered excerpts, character counts
and raw timings. Every report records source, fixture and benchmark hashes.
Run revisions in separate processes with the same script and fixtures:

```bash
python benchmarks/search_relevance.py \
  --package-root /path/to/baseline/packages/tofu-search \
  --repeats 9 --output /tmp/search-baseline.json
python benchmarks/search_relevance.py \
  --package-root packages/tofu-search \
  --repeats 9 --output /tmp/search-candidate.json
```

The benchmark contains 25 adversarial cases: body position, four languages,
constraint/navigation bait, small pools, complementary facts at three context
budgets, quoted literals, version identifiers and technical-symbol distinctions.
These are regression probes,
not an estimate of real-world search quality. Literal recall cannot grade
paraphrases or establish that a source is true. A live comparison must measure
retrieval coverage, stale/blocked sources, source correctness, latency and
downstream task completion with a fixed agent/model and comparable budgets.
Neither this benchmark nor green unit tests establish community leadership.

`benchmarks/search_html_reference.py` scores previously frozen `<case>.md`
outputs against a local Mozilla Readability `test-pages/<case>/expected.html`
corpus, retaining source/reference/output hashes and optional input-manifest
hashes. It performs no retrieval or model calls. Missing output cases remain
in the denominator as failures; incomplete reference corpora refuse scoring.

**Problem:** inserting a separator between every HTML text node splits words
across inline tags and can penalize faithful extraction. **Decision:** metric
version 2 separates DOM blocks but preserves inline continuations, applying
the same HTML text rule to references and rendered Markdown. It records parser
versions and refuses to overwrite reports. **Alternatives rejected:** stripping
all tags without block boundaries joins separate words; editing scores or
reference labels after a result obscures the measurement error. Retain older
reports under their original definitions and rescore identical bytes separately.

```bash
python benchmarks/search_html_reference.py \
  --corpus /tmp/readability-corpus --outputs /tmp/frozen-extractions \
  --out /tmp/html-word-overlap-v2.json
```

This is Unicode word overlap, not semantic entailment or field completeness.
It does not model CSS/JavaScript visibility or segment CJK into linguistic
words. Article-oriented references and an average score can hide omitted facts
or extra bylines; inspect individual cases and preserve separate agent results.
Verify `tests/meta/test_search_html_reference.py` before comparing measurements.

### Agent retrieval diagnostic

[`evaluations/search_quality`](../../../evaluations/search_quality/README.md)
compares free Tofu/DDGS retrieval through the application's existing agent
runtime. It freezes four multilingual public tasks, one model/provider,
identical search/fetch schemas and budgets, and a common Tofu fetcher.
Success requires observed source quotations and reference matches, with actual
dispatch and tool-surface evidence. Model accounting, unavailable search,
unsupported citations and incomplete runs remain separate observations.
The default command is offline; live model/network execution requires `--live`.
This small retrieval diagnostic does not certify product-wide superiority.

The separate [community source probe](../../../evaluations/search_quality/source-probe.md)
uses pinned external questions and URL annotations, with topic-balanced sampling
before retrieval. It measures recorded-source coverage and optional first-result
fetch availability through the same free workers. It uses no reference answers
or model grading; neither a source hit nor nonempty text establishes an answer.
