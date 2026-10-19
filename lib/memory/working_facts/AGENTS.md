# Conversation working facts

This owner records short-lived task knowledge in durable conversation scope.
It is separate from reusable long-term memory and shares the context composer,
selected-store CAS and existing artifact/review authorities.

`contract.py` owns wire validation; `repository.py` owns owner-scoped CAS;
`sources.py` resolves exact source versions; `service.py` owns claim revisions,
dependency evaluation and owner-only correction. `file_sources.py` admits only
host-issued witnesses, seals selected quotes in the same CAS and rechecks physical
files through the shared artifact snapshot guard. `tool_sources.py` seals selected
historical canonical observations; executor/settlement witnesses live in
`lib/tasks_pkg/working_tool_sources.py`. No model text grants approval.
`consumers.py` records prepared/provider-confirmed task dependencies through the
same record CAS. `lib/tasks_pkg/context_dependencies.py` binds common prompt and
provider boundaries; composer acquisition stays read-only. Verify uncertain
delivery, exact-set confirmation, targeted impact and owned pagination.
`dependencies.py` owns the shared source/revision assessment for consumers and
artifact review. `query_dependency_facts` resolves selected graphs in one read;
criterion sources read requirements only, never a full review of those facts.
Optional criterion `targetKind: turn` resolves through the owned ordinary Turn
requirement port; omission retains artifact semantics. Verify scope and selective
invalidation with `tests/memory/test_working_turn_criteria.py` on both stores.

Run `tests/memory/test_working_tool_sources.py`,
`tests/tools/test_tool_result_query_backends.py`, `tests/memory/test_working_file_sources.py`, `tests/memory/test_working_facts.py`, gateway/context tests and selected-store
restart cases before changing these boundaries. Behavior belongs to
`docs/contracts/WORKING_FACTS.md`, schema to `contracts/working_facts_v1.schema.json`.
