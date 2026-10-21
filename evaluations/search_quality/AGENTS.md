# Search quality diagnostic

`contracts.py` owns default public questions, budgets, citation-host policies
and conservative evidence predicates, including the shared optional fetch-query
schema. `runner.py` registers frozen task packs
and transports the native application API through the
existing HTTPRuntime; `worker.py` executes disposable free retrieval operations.
Read `README.md` for scope, invocation, cost interpretation and evidence limits.

Changing questions, predicates, adapters or budgets starts a new cohort.
Keep reference answers out of model prompts and equal tool schemas in both arms.
Never promote these diagnostic cohorts into a search leadership claim.

`source_corpus.py` pins external source annotations; `source_probe.py` reuses
the disposable workers for a separate model-free coverage probe. Read
`source-probe.md`; reference-URL hits are not answer or relevance grades.
Verify `tests/evaluations/test_search_source_probe.py` for this entry point.

Verify `tests/evaluations/test_search_quality.py`,
`tests/evaluations/test_search_focused_reads.py`,
`tests/evaluations/test_search_candidate_projection.py` and the native HTTP tests in
`tests/evaluations/test_programmatic_pilot.py`; the default CLI is offline.
