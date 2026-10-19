# Rich response and artifact presenters

`renderer.ts` owns typed rich-response rendering. `artifact-delivery.ts` and
`artifact-review.ts` are typed presenters mounted by the retained artifact
panel; keep preview markup outside the application document and release each
mount on close or version/library switch.

Artifact review consumes `api/artifact-review.generated.ts`, generated from
`contracts/artifact_review_v1.schema.json`. Its durable decisions belong to
the shared `lib/acceptance/review.py` and artifact/Turn target adapters, not
presentation state or task completion. `turn-review-outputs.ts` owns only
mount-local declared-slot and exact-version drafts; `mountTurnReview` reuses
the same presenter from `features/turn-review/`. Ordinary Turn behavior and
execution context are specified in `docs/contracts/ARTIFACT_REVIEW.md`.
Turn invalidation borrows the existing conversation observer. The shared
presenter coalesces background reads, fences them against explicit saves and
defers application while input, requirement or version drafts are active.
No polling or implicit evidence rebinding; verify `turn-review-live.test.ts`.
Supporting facts load lazily through the conversation API. Preserve selected
versions across refresh/conflict and abort picker work with the review mount;
verify source-change behavior in `test_visual_artifact_review_dependencies.py`.

Run this directory's Vitest tests and `tests/visual/test_visual_artifact_review.py`
for review changes. Use the frontend/browser skills for retained bridge edits.

Dynamic surfaces extend `contracts/rich_response_v1.schema.json`; regenerate via
`npm run generate:rich-response`. `layout.ts` composes stable block nodes;
`resource-client.ts` owns scoped, visible resource reads; `guidance-form.ts`
uses the existing Turn guidance adapter. Provider composition stays behind
`lib/response_surfaces/__init__.py` and the SDK host's public read port.
Run `surfaces.test.ts`, `tests/artifacts/test_response_surfaces.py`, and
`tests/visual/test_visual_dynamic_surfaces.py`; the gallery fixture is illustrative.
