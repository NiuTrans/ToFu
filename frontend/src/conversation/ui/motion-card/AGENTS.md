# Multimedia receipt presentation

`index.ts` composes the card shell. `labels.ts` owns localized verbs/purpose/source
chips; `validation.ts`, `media.ts`, and `production.ts` own their structured
receipt families. `markup.ts` escapes scalar values and bounds preview lists;
`payload.ts` decodes the handler envelope. `contracts.ts` describes presentation
inputs and ports, not a duplicate backend admission schema.

No content owner imports the facade or reads runtime globals. The retained
`ui/tool_rounds_rich.js` bridge supplies localization and WorkUnit scan demand
until core dispatch migrates. Do not add transport, timers or listener owners.
Never infer success or a durable WorkUnit handle from a payload task id; preserve
failure/rollback errors and the explicit acceptance metadata.
Accepted jobs delegate their visible shell to `../work-unit/shell.ts`; the
WorkUnit owner handles mounted progress, reviews, previews and revision intents.
Ordinary multimedia receipts retain their existing disclosures.

Verify `node --test tests/motion-card.test.cjs`, neighboring rich-tool harnesses,
the visual multimedia receipt fixture, then `npm run check:frontend`.
