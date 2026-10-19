# Production work cards

`shell.ts` renders an explicit accepted-task handle. `tracker.ts` owns the one
mounted-card request/observer lifecycle; `view.ts` projects snapshots through
`gate.ts` and `media.ts`. `labels.ts` owns human stage/status labels; `contracts.ts`
describes presentation inputs and injected API/identity ports. `composer.ts`
preserves the existing human draft when requesting a revision.

Server WorkUnit snapshots remain the lifecycle authority. A pending human gate
is interaction state, not a new task status. Terminal mounted cards retain no
poll timer. The last removed card releases its observer, views and media;
late owner/entry receipts cannot repaint or issue a follow-up mutation.

Verify `tests/work-unit.test.cjs`, multimedia/slide-editor and tool-group
neighbors, the production-card browser fixture, then `npm run check:frontend`.
