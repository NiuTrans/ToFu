# Community source coverage

**Problem:** four development tasks and synthetic ranking fixtures cannot show
how often free retrieval reaches independently recorded sources across topics.
**Decision:** `source_corpus.py` pins the official Google SimpleQA Verified CSV
and selects an equal number of rows per topic by SHA256(seed:row_id), before
any search. `source_probe.py` sends each complete question to both existing
free-search workers, alternating arm order in fresh sequential processes.
It neither rewrites queries nor supplies reference URLs to workers.

The [official dataset card](https://huggingface.co/datasets/google/simpleqa-verified/blob/ed392714d14fa5225b07c2b456453983bc75f690/README.md)
describes a **tool-free knowledge benchmark**. This is a different diagnostic
using its source annotations: answers and the official model grader are not
used, and these results must never be called a SimpleQA score. The pinned
release has 1,000 English rows, ten topics and an MIT license. Raw data remains
outside Git. The exact revision, CSV digest, population and sampling rules live
in `source_corpus.py`; changing them starts a new cohort.

**Alternatives rejected:** applying a literal answer matcher to numeric ranges
and aliases would manufacture grading failures; using the official benchmark
name for a tool-assisted run would misrepresent its purpose. Another agent
loop or network adapter would confound the retrieval comparison. This probe
reuses `runner.Workers`, bounded `Artifacts`, the public search/fetch APIs and
the production process retirement owner. It makes no model calls.

## Measures and limits

- `reference_hit_at_1/6` and reference reciprocal rank use only the displayed
  result list. A hit means a recorded URL was returned, not that its content
  is true, current, relevant or sufficient to answer the question.
- URL identity uses the existing `contracts.url_key`: host case/default ports
  and fragments normalize; schemes, paths and queries remain distinct. No
  inferred redirect, `www` alias or trailing-punctuation repair is credited.
  The dataset's source annotations may be stale, malformed or incomplete.
- Exact reference-host presence is a diagnostic hint, never document credit.
  Opt-in raw engine URL traces distinguish a missing displayed source from
  one observed upstream. Traces are capped at 256 entries and 64 KiB, preserve
  whole URLs, and never authorize fetches. The two arms have different raw
  upstream pool sizes; only their displayed results share the comparison cap.
- With `--fetch-top 1`, both arms fetch their first displayed URL through the
  same Tofu implementation. Nonempty output measures availability only, not
  source completeness, relevance or answer accuracy. Failed searches cannot
  supply fetch targets, even if a malformed response also carries result rows.
- Timeouts and tool failures remain in the recorded denominator. An unproven
  cleanup or worker infrastructure failure stops later dispatch. Raw receipts
  are written before interpretation; incomplete or changed-source/data cohorts
  cannot be marked comparable. Existing output directories are never reused.

The default sample is two rows per topic, twenty questions and forty searches.
An optional first-result fetch adds at most forty fetch calls. The existing
six-result, 1,800-character snippet, 20,000-character fetch and 45-second worker
bounds apply. There are no infrastructure retries. Topic-balanced aggregates
are not population-weighted estimates, and public data is not contamination-free.
One literal search is not a measure of adaptive agent browsing or leadership.

## Invocation

Obtain the [pinned CSV](https://huggingface.co/datasets/google/simpleqa-verified/resolve/ed392714d14fa5225b07c2b456453983bc75f690/simpleqa_verified.csv)
outside the repository. The loader checks its complete byte digest and
population before sampling. Default invocation is offline:

```bash
python -m evaluations.search_quality.source_probe \
  --corpus /tmp/simpleqa_verified.csv --per-topic 2 --seed 20261021 --fetch-top 1
```

For live execution, privately configure `TOFU_SEARCH_EVAL_PROXY` (an explicit
empty value means direct), use the verified Tofu and pinned DDGS interpreters,
and choose a new absolute output directory:

```bash
python -m evaluations.search_quality.source_probe --live \
  --corpus /tmp/simpleqa_verified.csv --per-topic 2 --seed 20261021 --fetch-top 1 \
  --tofu-python /path/to/tofu-python --ddgs-python /path/to/ddgs-python \
  --output /tmp/community-source-cohort
```

The plan is saved before preflight and queries. The manifest adds actual worker
identities and a proxy hash; it never stores proxy credentials. Per-operation
directories retain search/fetch receipts and compact records. `summary.json`
reports completion, comparability, failures, per-topic source hits and raw
timings; missing records are not silently removed from the planned population.

Verify `tests/evaluations/test_search_source_probe.py` and
`test_search_candidate_projection.py`, then the existing search diagnostic and
native HTTP contracts. This does not replace the four-task agent diagnostic.
