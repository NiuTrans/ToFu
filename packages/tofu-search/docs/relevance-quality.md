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

## Ranking and excerpts

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
