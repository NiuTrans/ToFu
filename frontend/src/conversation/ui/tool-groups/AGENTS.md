# Tool-group UI owners

`index.ts` composes `context.ts` (per-render membership/title indexes),
`batch.ts` (batch chrome), `slot.ts` (rich routing), `panel.ts` (counts/tail),
`narration.ts` (translated prose) and `inspector.ts` (evidence anchors).
`contracts.ts` defines display inputs and injected collaborators. Grouping
and title derivation remain pure presentation owners; retained callbacks own
Swarm, individual tool bodies and task lookup. A session lasts one render.
Standalone immutable canonical arrays retain weak title memoization.
Run `tests/tool-group-presentation.test.cjs`, adjacent timeline/tool-render
contracts, `npm run check:frontend`, and the actual-browser tool-group gate.
