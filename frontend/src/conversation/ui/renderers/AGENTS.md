# Conversation content renderers

## Owner map

`../classic-conversation-renderers.ts` composes dispatch and disposal.
This directory owns content projection into the Surface-provided node:

| Module | Responsibility |
|---|---|
| `ports.ts` | Explicit Markdown, copy, media, rich markup and I/O interfaces |
| `context-blocks.ts` | Origin and context fold/rail |
| `plan-blocks.ts` | Proposed-plan and execution evidence |
| `compaction-block.ts` | Compaction receipt and shared token-flow subprojection |
| `text-blocks.ts` | Prose, reasoning disclosure, translation and live status |
| `tool-blocks.ts` | Tool, program and injection projection |
| `activity-block.ts` | Chronology-local activity rows |
| `failure-receipt.ts` | Selectable diagnostics and document-scoped copy lifecycle |
| `system-note-blocks.ts` | Generated reason taxonomy and context-note groups |
| `change-receipt-blocks.ts` | File changes, provenance and rollback receipts |
| `media-blocks.ts` | Generated images, artifacts and attachments |
| `workflow-blocks.ts` | Branch, queue and concluded goal-run surfaces |
| `turn-chrome.ts` | Avatar, header, action datasets and footer; accepted task time delegates to `../task-time-presentations.ts` |
| `stable-dom.ts`, `icons.ts`, `localized-copy.ts` | Shared reconciliation and presentation primitives |

## Dependencies and verification

Import the narrow collaborator directly; never import the composition facade.
Content dependencies stay acyclic. Activity may reuse compaction subprojection;
workflow may reuse background-result markup. No content family owns transport,
durable state, scroll policy or another renderer registry.

Run `npm run check:renderer-boundaries` and the focused renderer tests in `tests/frontend/`, `tests/background/` and
`tests/tasks_pkg/test_stream_phase_i18n.py`, then `npm run check:frontend`.
Use the owning real-browser visual tests for focus, disclosure, responsive
geometry or lazy loading. See `../../README.md#renderer-ownership` for the
domain-level contract and exact behavioral evidence.
