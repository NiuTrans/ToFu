# Free search in a real agent

Problem: engine availability and synthetic ranking scores do not establish
whether an agent can find supported answers. Decision: use one configured
application's production agent loop with exactly two client tools, swapping
only the free retrieval implementation. The common fetcher is tofu-search.
Alternatives rejected: writing another agent loop, comparing different models,
or treating an answer from model memory as successful retrieval.

This is a four-task diagnostic in English, Chinese, Japanese and Spanish.
It compares Tofu's default free web retrieval, without automatic page fetching
or an LLM filter, against [DDGS 9.16.0](https://pypi.org/project/ddgs/9.16.0/).
DDGS uses its automatic engine selection and each task's language region.
Both return six results in the same title/URL/snippet format, with the same
1800-character snippet ceiling. Both use the same explicit network proxy and
verified TLS. No search API key or paid search service is required.

The agent must discover URLs by searching, then fetch them before citing.
Follow-up searches are allowed; arbitrary guessed URLs are not. This measures
retrieval within that constrained workflow, not the products' full extraction,
browser, cache or agent capabilities. Each operation starts a fresh process;
cold startup and owned process cleanup are included in tool wall time.

The default command prints a manifest and makes no network/model calls:

```bash
python -m evaluations.search_quality.runner
```

Use separate Python environments with the pinned DDGS version and current
tofu-search dependencies. An existing admitted model/provider and a running
loopback application are required. Live calls use its existing authorization;
the evaluator neither imports credentials nor changes routing configuration.
The manifest records base extraction dependencies and whether optional browser
and PDF packages are installed. Both arms share that exact fetch environment;
different dependency profiles belong to separate cohorts. Inspect resolved
environment paths before installing packages: a temporary environment can have
a `site-packages` link into the shared checkout.

```bash
# Configure TOFU_SEARCH_EVAL_PROXY privately; an explicit empty value means direct.
python -m evaluations.search_quality.runner --live \
  --creator CREATOR --model MODEL --provider PROVIDER \
  --tofu-python /path/to/tofu-python --ddgs-python /path/to/ddgs-python \
  --origin http://127.0.0.1:15000 --output /tmp/search-agent-cohort \
  --trials 1 --timeout 180
```

For authenticated deployments, `--api-token-env NAME` reads the application
token from the named environment variable. It is never written to artifacts or
forwarded to search workers. A platform needing a separate native library
directory for the Tofu worker may set
`TOFU_SEARCH_EVAL_TOFU_LD_LIBRARY_PATH`; the controller and DDGS worker retain
their own environment.

The frozen limits are eight tool calls, 2048 output tokens per model request,
60000 reported tokens per task, a 45-second tool deadline, and the chosen total
task deadline. A model can consume multiple requests within those limits.
HTTP reconciliation and joined process cleanup can add bounded time after
useful work stops; records include that wall time.
Search operations cost no API fees; model use is separate. Records preserve
the application's canonical token/cost accounting and actual dispatch identities.
Missing accounting stays unknown; reported cost is not a provider invoice.

The manifest precedes paired execution. It includes task/prompt/config/source
hashes, dependency identities, proxy hash, budgets, order and acceptance rules.
Tasks alternate arm order between cases and repetitions. No infrastructure
retry removes a failed attempt. The native HTTP adapter verifies tool exposure,
follows producer cursors, owns callback receipts and reconciles ambiguous
admission. The production process retirement helper kills and joins owned
workers and descendants on timeout. Unsettled work stops later dispatch.

Each fact needs an exact reference value, an allowed primary hostname, and an
8–350-character quotation actually delivered by a successful fetch of a
search-discovered URL. Whitespace and Unicode compatibility normalization are
allowed; paraphrases are not. Quote predicates check reference terms, not
semantic entailment. Wrong-page citations, rejected callbacks, missing or
changed actual model/provider identity and changed paired tool fingerprints
cannot certify a comparable success. Inspect individual fact flags and raw
records before interpreting differences.

Contract v2 accepts either JSON alone or one enclosing JSON code fence with no
surrounding prose. Raw JSON compliance is reported separately. Each user question
contains the exact empty output structure; reference answers and URLs stay out
of prompts. Retrieved primary pages and reference-term coverage are separate
metrics, not substitutes for a supported final answer.

Problem: the first diagnostic mixed presentation failures with retrieval and
quotation failures. Decision: freeze these distinctions before a new cohort,
keep the original cohort's grades, and reject ambiguous prose/multiple answers.
Alternatives rejected: retroactively changing old scores or accepting arbitrary
field aliases and reconstructed quotations. Worker preflight verifies the
imported Tofu source tree, and the closing receipt checks that measured source
files did not change during the cohort.

Problem: a checkout's ignored `egg-info` can report an old distribution version
even while the worker imports current source. Decision: use that source's
matching `pyproject.toml` declaration for the Tofu version and include it in the
cohort fingerprint. Installed dependencies still use distribution metadata;
source hashes remain the code-identity check. Alternatives rejected: trusting
the stale label, silently repairing shared installation metadata, or relabeling
previous cohort manifests and grades.

Contract v3 gives each native client handoff the total task waiting budget,
while each actual search/fetch operation retains its 45-second limit.
Problem: a handoff can already be waiting on the server while the sequential
client handles an earlier operation; a 55-second callback timeout then expires
even though both operations fit the task budget. Decision: include queue time
in callback waiting, with the existing controller owning deadline abort and
joined cleanup. Alternatives rejected: increasing network-operation limits,
silently retrying expired callbacks, or counting that harness failure as poor
search quality. Earlier cohorts keep their original grades and invalid pairs.

Contract v4 preserves Tofu's uncertain follow-up candidates after primary
results, within the same six-URL budget. Both arms use one projection for
title/snippet limits and URL admission. A candidate's `evidence_status`
distinguishes `search_snippet` from `unverified_candidate`; neither establishes
a fact without a successful fetch and observed quotation. Original URL strings
are retained, duplicates share one slot, and invalid or over-budget URLs are
omitted rather than truncated into a different fetch target. The projection
includes allowlisted reason/count/deadline diagnostics, never provider messages.

Problem: iterating only the primary list discarded metadata-held candidates
that the native tool presents for follow-up, making some searches appear empty.
Decision: preserve their discovery authority and uncertainty in the common
adapter, without changing ranking, tool schemas or budgets. Alternatives
rejected: treating uncertain snippets as verified evidence, guessing canonical
URLs, or changing earlier cohort grades. Quote length validity is now reported
separately from literal source membership; both remain required. This separates
an overlong observed quotation from an invented one without weakening grading.
Verify `tests/evaluations/test_search_candidate_projection.py` alongside the
existing runner and native HTTP contracts before a new cohort.

Reference sources, checked 2026-10-21:

- [Python 3.13 TaskGroup](https://docs.python.org/3.13/library/asyncio-task.html)
- [SQLite JSONB](https://www.sqlite.org/json1.html)
- [National Museum of Nature and Science, Ueno admission](https://www.kahaku.go.jp/riyou/nyukan-annai/)
- [Museo Reina Sofía hours](https://www.museoreinasofia.es/visita/horarios-tarifas/)

Museum schedules may change. Revalidate those references before later cohorts;
do not silently change a failed cohort's oracle. The running application's source
revision is not certified by the client source hash. These tasks, one application,
one model and one network path establish no population advantage or leadership.

Offline verification:

```bash
python -m pytest -o addopts='' tests/evaluations/test_search_quality.py \
  tests/evaluations/test_programmatic_pilot.py -k 'not public_runtime'
```

The excluded standalone SDK/program interpreter tests belong to the neighboring
runtime suite; they are not used by this HTTP retrieval diagnostic.
