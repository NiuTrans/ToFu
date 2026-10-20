# Search quality diagnostic

`contracts.py` owns frozen public questions, budgets and conservative evidence
predicates. `runner.py` transports the native application API through the
existing HTTPRuntime; `worker.py` executes disposable free retrieval operations.
Read `README.md` for scope, invocation, cost interpretation and evidence limits.

Changing questions, predicates, adapters or budgets starts a new cohort.
Keep reference answers out of model prompts and equal tool schemas in both arms.
Never promote these four tasks into a search leadership claim.

Verify `tests/evaluations/test_search_quality.py`,
`tests/evaluations/test_search_candidate_projection.py` and the native HTTP tests in
`tests/evaluations/test_programmatic_pilot.py`; the default CLI is offline.
