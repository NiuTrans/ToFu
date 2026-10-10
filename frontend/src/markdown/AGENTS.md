# Markdown rendering guidance

This directory owns the Markdown pipeline; parser installation stays in
`../markdown-policy.ts`. Read `docs/RENDER_CONTRACT.md` for user behavior.

- `index.ts` composes injected ports, environment invalidation and lifecycle.
- `prepare-source.ts`, `fences.ts`, `table-repair.ts` normalize source without DOM.
- `html-policy.ts` owns sanitizer, navigation and math-output ordering.
- `code-blocks.ts` owns scratch DOM and mounted lazy-language completion.
- `render-cache.ts` owns exact-source LRU identity and shared variant capacity.
- `deferred-enrichment.ts` owns the temporary selection listener; clear queued
  work when the renderer changes document or is disposed.
- Retained `core/markdown.js` supplies runtime ports and delegated copy/fold
  actions only. Do not restore pipeline logic or cache state to that bridge.
- Streaming bypasses cache/highlighting/math layout. Clear cached/owned state on
  disposal; detached scratch nodes never stand for mounted content.

Verify with `markdown-rendering.test.cjs`, Markdown frontend contracts, then
`check:frontend` and the conversation-reading browser gate.
