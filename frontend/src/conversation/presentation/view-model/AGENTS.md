# Conversation view-model projection

## Owner map

`../conversation-view-model.ts` is the public composition port. It joins lanes,
attempts, queues and presentation overlays, and memoizes eligible settled Turns.
Its type re-exports preserve existing callers; projection modules import types
directly from `types.ts` and never re-enter the facade.

| Module | Responsibility |
|---|---|
| `types.ts` | Block, Turn, lane, local display and diagnostics contracts |
| `turn-blocks.ts` | Ordered projection pipeline and weak per-Turn block memo |
| `segment-blocks.ts` | Durable segment conversion and tool occurrence matching |
| `program-blocks.ts` | Linear program-parent placement with durable chronology |
| `projection-sidecars.ts` | Attachments, context, receipts, plans and anchored activity |
| `activity-blocks.ts` | Activity visibility, validation and rejection evidence |
| `block-coalescing.ts` | Adjacent thinking/notes and settled activity presentation |
| `translation-blocks.ts` | Translated plan and execution display projection |
| `turn-metadata.ts` | Header metadata and authoritative action capabilities |
| `live-turns.ts` | Live attempt presentation and durable autopilot-run notices |
| `projection-values.ts` | Safe record/text narrowing and block identity claims |

## Dependencies and verification

Projection is DOM-free and cannot mutate TurnStore, source arrays or DTOs.
Internal dependencies form a DAG enforced by `npm run check:renderer-boundaries`.
External activity anchors and round outlines import the type owner directly
and participate in the same dependency check. Cache eligibility and
keys belong to the composition or pipeline owner; never add a second cache for
the same projection.

Run the co-local `program-blocks.test.ts` and `turn-memo.test.ts`, then
`tests/frontend/test_frontend_conversation_surface_vite.py` and
`tests/conversation/test_conversation_view_model_note_grouping.py`, followed by
`npm run check:frontend`. Changes to visible chronology or branches also need
the real-browser round, activity and navigation checks under `tests/visual/`.
The lifecycle and measured work contract lives in `../../README.md#view-model-ownership`.
