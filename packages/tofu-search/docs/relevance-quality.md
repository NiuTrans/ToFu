# Search evidence quality

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
Ranking also applies when the candidate pool is smaller than the requested
count. Existing primary-source, engine-agreement and diversity signals remain.
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

The benchmark contains 19 adversarial cases: body position, four languages,
constraint/navigation bait, small pools, complementary facts at three context
budgets, quoted literals and version identifiers. These are regression probes,
not an estimate of real-world search quality. Literal recall cannot grade
paraphrases or establish that a source is true. A live comparison must measure
retrieval coverage, stale/blocked sources, source correctness, latency and
downstream task completion with a fixed agent/model and comparable budgets.
Neither this benchmark nor green unit tests establish community leadership.
