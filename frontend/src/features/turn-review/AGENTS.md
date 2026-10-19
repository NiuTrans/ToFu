# Ordinary Turn review

`presentation.ts` owns one lazy dialog and generation fences. The retained
conversation surface bridge activates it at navigation, observes Turn removal,
and disposes it with the shell. Its one mounted execution stamp invalidates
the shared review on committed Attempt, model-context or status changes;
unrelated Turns and display-only revisions do not trigger reads.
`dialog.ts` only mounts the UI-kit dialog.

Requirement drafts, evidence and output-version selection belong to the shared
`rich-response/artifact-review.ts` presenter and `turn-review-outputs.ts`.
Acceptance, scope and identity are server authority; use the typed v4 facade.
Contract: `docs/contracts/ARTIFACT_REVIEW.md`.

Verify mount retirement in `presentation.test.ts`, shared drafts/version binding
in `rich-response/turn-review{,-live}.test.ts`, and running/completed ordinary Turns in
`tests/visual/test_visual_turn_review.py` with the real application.
