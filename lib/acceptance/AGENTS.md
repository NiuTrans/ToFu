# Owner acceptance map

`review.py` owns requirement generations, decision projection, fact admission
and selected-store record CAS for artifact and ordinary Turn adapters.
`dependencies.py` delegates current evidence to the working-fact graph.
`contract.py` loads the single wire authority under
`contracts/artifact_review_v1.schema.json` (including the Turn definitions).

`turns.py` binds judgments to one owned main-lane assistant Turn and explicitly
declared output slots. It reads lifecycle and artifacts through their facades;
it never executes tasks or treats model completion as owner acceptance.
The artifact adapter remains `lib/artifacts/review.py`.
`continuity.py` validates a server-accepted continuation's flat original review
identity through lifecycle facades. Its private schema/reserved key live in
`contracts/conversation_execution_identity_v1.json`; no copied review store or
execution authority. Verify `test_turn_review_continuity.py` and the production
Goal correction/restart fixture alongside ordinary review/context tests.

Behavior, alternatives and limits: `docs/contracts/ARTIFACT_REVIEW.md`.
Verify `tests/artifacts/test_turn_review.py`, artifact review/dependency suites
on both stores, endpoint/OpenAPI guards and the generated browser DTO check.
