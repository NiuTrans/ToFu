# Turn-block disclosure semantic

Audience: a language model (or engineer) reasoning about how a rendered
conversation Turn's collapsible blocks behave across re-render. One-hop
reference: pointers to the single sources of truth (SSOT), not restated logic.

Wire/authority (do not duplicate here):
- Turn → segment → block wire shape: `contracts/conversation_sync_v3.yaml`
  (generated TS: `frontend/src/api/conversation-sync.generated.ts`), described
  in `docs/contracts/API_CONTRACT.md` and `docs/contracts/EVENTS.md`.
- Typed projection SSOT (Turn → blocks):
  `frontend/src/conversation/presentation/conversation-view-model.ts`.
- Typed DOM SSOT (blocks → keyed elements + render skip):
  `frontend/src/conversation/ui/conversation-surface.ts`.
- Typed per-kind renderer SSOT (default open/closed + body HTML):
  `frontend/src/conversation/ui/classic-conversation-renderers.ts`.
- Legacy settled-timeline mirror (parity, not authority):
  `frontend/src/runtime/sections/ui/tool_rounds.js`.
- Executable specs: `tests/conversation/test_conversation_view_model_note_grouping.py`,
  `tests/context/test_system_note_context_grouping.py`.

The disclosure mechanism is a native `<details>` element. "Default state"
below is whether the renderer sets `details.open` at construction. A renderer
that never sets `.open` produces a COLLAPSED disclosure.

## 1. Block inventory

DOM identity = `blockId` (`ConversationBlockBase.blockId`,
`conversation-view-model.ts:87`), stamped onto the wrapper
`<section data-block-id>` in `renderBlocks`
(`conversation-surface.ts:1213-1215`). The wrapper is keyed by `blockId`;
the `<details>` lives inside it.

| kind | default state | open-state survival mechanism |
|---|---|---|
| `text` | n/a (no disclosure; inner background-completion `<details>` collapsed, `classic-conversation-renderers.ts:1114`) | wrapper reuse; bilingual `<details>` reused in place (`:1404-1407`) |
| `thinking` | **OPEN while streaming, CLOSED when terminal** — created `open = !block.terminal` (`:1213`), auto-collapses once on terminal (`:1224`) | element reused (no `replaceChildren`); a manually-opened complete block "stays put" (comment `:1219-1222`) + `sameBlock` |
| `tool` | rich: per body; fallback card preserves prior | rich: capture/restore + Swarm keyed reconcile via `setManagedRichHtml` (`:1464`); fallback: `priorOpen` read-back (`:1467-1469`, `:1484`) |
| `program` | same as `tool` | rich via `setManagedRichHtml` (`:1498`); fallback `priorOpen` (`:1501`, `:1529`) |
| `attachments` | n/a (media grid, no `<details>` observed) | `sameBlock` |
| `injections` | rich: per body; fallback **collapsed** (`:2103`, never sets open) | rich via `setManagedRichHtml` (`:2101`); fallback `sameBlock` only |
| `provenance` | rich: per body; fallback **collapsed** (`:2224`, never sets open) | rich via `setManagedRichHtml` (`:2215`); fallback `sameBlock` only |
| `system-note` | **collapsed** — `renderSystemNoteBlock` never sets open (`:2025-2039`) | `sameBlock` arm (`conversation-surface.ts:784-787`) |
| `system-note-group` | **collapsed** — `renderSystemNoteGroupBlock` never sets open (`:2053-2093`) | `sameBlock` arm (`conversation-surface.ts:788-795`) |
| `file-changes` | **OPEN iff `files.length <= 5`, else collapsed** (`:2136`) | explicit `previousDetails?.open` read-back (`:2129-2131`, `:2136`) |
| `origin` | n/a (banner, no `<details>`) | `sameBlock` |
| `context` | n/a (fold/rail; `node.hidden`, not `<details>`, `:421-447`) | `sameBlock` |
| `compaction` | **collapsed** (`:670`, never sets open) | `sameBlock` (`conversation-surface.ts:826-828`) |
| `image-generation` | n/a (media card) | `sameBlock` |
| `rolled-back` | **collapsed** ("Collapsed by default" comment, `:2258`; never sets open) | `sameBlock` |
| `proposed-plan` | n/a — `<article>` card, always visible (`:493`) | `sameBlock` |
| `plan-execution` | **collapsed** (`:543`, never sets open) | `sameBlock` |
| `artifacts` | n/a (list) | `sameBlock` |
| `autopilot-run-notice` | inner report **collapsed** (`:2563`, never sets open) | `sameBlock` |
| `activity-event` | **OPEN** when it carries a terminal error — `open = previousOpen ?? true` (`:1728`); plain rows are `<div>`s | `previousOpen` read-back (`:1889`) |
| `queue-status` | n/a (row) | default `sameBlock` arm (`conversation-surface.ts:848`) |
| `live-status` | n/a (status line) | `sameBlock` |

Note: `system-note`, `system-note-group`, `provenance` (fallback),
`rolled-back`, `compaction`, `plan-execution`, `injections` (fallback),
`autopilot-run-notice`, and the background-command result all do
`node.replaceChildren(details)`. They preserve the reader's open state
through a shared read-back, `preserveDetailsOpen(node, defaultOpen)`
(`classic-conversation-renderers.ts`), which reads the live `<details>`
before the rebuild and is applied to the rebuilt disclosure. The default
applies only when no prior disclosure exists (first paint / renderer-mode
switch). `rolled-back` preserves each of its two disclosures independently
by variant class.

## 2. The system-note rule

Stated crisply (typed path; legacy mirror is parity):

- A maximal run of **adjacent** prompt `system-note` blocks accepted by
  `presentation/context-note-grouping.ts::isContextInjectionNote` coalesces
  into ONE
  `system-note-group` block (a single collapsed `sw-note-group` disclosure
  with a per-item list inside), in `coalesceAdjacentSystemNoteBlocks`
  (`conversation-view-model.ts:1466-1506`), invoked from `selectTurnBlocks`
  (`:1610`).
- Grouping is **display-only**: member notes keep their wire positions and
  content; the group takes the **first member's `blockId`** as its DOM
  identity so a live-growing run re-renders in place
  (`:1494-1502`). Grouping never crosses a non-note block
  (interface comment `:173-175`; function comment `:1456-1465`).
- A run of exactly **one** context note is never wrapped — it keeps its solo
  `system-note` row (`:1491-1493`).
- Reasons `active_checklist` / `modified_files` are INTERVENTIONS
  (excluded by the shared `isContextInjectionNote` predicate)
  — they wear the `context-injection` noteKind but keep their **own
  first-class row** and never join the anonymous group (filter
  `:1473-1477`). Every other `noteKind` (`intent-stall`,
  `todo-continuation`) also keeps its own row. Legacy `system-intervention`
  notes join a group only for known session context reasons with prompt action;
  unknown reasons and stop/continue/tool-policy actions stay first-class.
- The render body is the **verbatim wire text, escaped, never markdown**:
  `systemNoteBodyCard` sets `pre.textContent = block.text`
  (`classic-conversation-renderers.ts:2013`); the view-model carries
  `text: segment.text` unchanged (`conversation-view-model.ts:728`).
- Legacy mirror parity: `_groupSegContextNotes` (`tool_rounds.js:1530-1553`),
  `_renderSegSystemNoteHTML` (`:1460-1480`, `<pre>` + `escapeHtml` at `:1478`),
  `_renderSegSystemNoteGroupHTML` (`:1510-1524`), reason map
  `_SEG_NOTE_REASON_PRESENTATION` (`:1398-1401`), intervention set
  `_SEG_NOTE_REASON_INTERVENTIONS` (`:1402-1406`), applied inside
  `renderSegmentTimelineHTML`/`_renderTimelineBatch` after batching
  (`:1560`, `:1591-1594`).

Executable specs:
- Typed: `tests/conversation/test_conversation_view_model_note_grouping.py`
  — run-of-3 → one `system-note-group` with first-member `blockId`, members
  in wire order, verbatim text, preserved reasons; lone note stays solo; an
  `system-intervention` breaks the run; an `active_checklist` reason breaks
  the group (assertions in the `_HARNESS`, ≥11 PASS gate).
- Legacy: `tests/context/test_system_note_context_grouping.py` — same merge
  policy plus one group row at the first member's wire slot, a `{n}` count
  pill, no leftover context sibling rows, intervention keeps its own row,
  deliverable excluded (≥16 PASS gate).

## 3. The core invariant and its three mechanisms

**Invariant: a default-collapsed disclosure must NEVER re-open (or snap
shut) as a side effect of a sibling block's re-render.** A block's open
state is user intent; re-rendering an unrelated block — or re-rendering this
block because an *unrelated field* changed — must not reset it.

Three mechanisms preserve open-state:

1. **`sameBlock` short-circuit** (`conversation-surface.ts:720-848`, applied
   at `:1220-1229`). `renderBlocks` keys children by `data-block-id`
   (`:1203`); when `sameBlock(prev, next)` is true and the renderer is
   unchanged, the renderer is **not called**, so the existing `<details>`
   (and its `open`) is untouched. This is the *only* protection for the
   `replaceChildren`-style disclosures (system-note, group, compaction,
   provenance-fallback, rolled-back, plan-execution, autopilot notice).
2. **Capture/restore around HTML replacement** —
   `captureInteractiveElementStates` (`classic-conversation-renderers.ts:812-832`,
   records `details.open` at `:822-823`) and
   `restoreInteractiveElementStates` (`:834-853`, re-applies `open` at
   `:841-843`), invoked by `setManagedRichHtml` (`:964-984`) around
   `node.innerHTML = html`. Used by the rich tool/program/provenance/
   injections paths.
3. **Keyed Swarm reconciliation** —
   `reconcileSwarmRichHtml`/`syncSwarmElementAttributes`
   (`classic-conversation-renderers.ts:861-894`, called at `:972`). It patches
   attributes in place and **never touches `DETAILS.open`** (`:869-870`,
   `:875-877`) and preserves reader-state classes
   (`SWARM_READER_STATE_CLASSES`, `:855-857`); when it returns true,
   `setManagedRichHtml` skips both the `innerHTML` clobber and the
   positional restore (`:973-982`).

A fourth, shared pattern now covers every `replaceChildren`-style
disclosure: `preserveDetailsOpen(node, defaultOpen)` reads the live
`<details>` before the rebuild and is applied to the new disclosure, so open
intent is sticky in BOTH directions (a manually-opened block stays open; a
manually-collapsed default-open block stays collapsed) across any re-render —
including the live-growing `system-note-group`, whose `sameBlock` arm
intentionally returns false when a member is added mid-turn. `file-changes`
(`previousDetails?.open`) and `activity-event` (`previousOpen ?? true`) keep
their own dedicated read-backs; `thinking` uses element reuse +
create-open/close-once. The paper/report reader's `toolZone.innerHTML`
rebuild (`frontend/src/runtime/sections/paper/report.js`) preserves note
disclosures by a stable `note:<kind>:<reason>` / `group:<kind>` key, since a
positional path is unstable when the group grows.

## 4. For the model — adding a new block kind

Exact checklist (all five, in order):

1. **View-model kind** — add the kind to `ConversationBlockKind`
   (`conversation-view-model.ts:68-90`), a `…BlockViewModel` interface
   extending `ConversationBlockBase`, the `ConversationBlockViewModel`
   union (`:294-317`), and construct it (with a `claimedBlockId`,
   `:591`) in the projection.
2. **`sameBlock` arm** — add a `previous.kind === '<kind>'` compare in
   `conversation-surface.ts` `sameBlock` (`:720-848`) listing every field
   that should force a re-render. This arm is what protects open-state for
   `replaceChildren`-style disclosures: if you omit a changing field, the
   block never re-renders; if the arm returns false spuriously, an open
   disclosure snaps shut.
3. **Renderer arm** — add `render<Kind>Block` in
   `classic-conversation-renderers.ts` and wire it into the block dispatch;
   if the body is rich HTML, route through `setManagedRichHtml` so
   capture/restore + Swarm reconciliation apply.
4. **Default state** — decide the default and encode it explicitly: set
   `details.open` at construction (e.g. `thinking` `:1213`,
   `activity-event` `:1728`, `file-changes` `:2136`) or leave it unset for
   default-collapsed (e.g. `system-note` `:2025`). If it is collapsible and
   rebuilds via `replaceChildren`, also preserve user intent (a `priorOpen`
   read-back, or rely on the `sameBlock` arm being exact).
5. **A test** — add an executable spec beside
   `tests/conversation/test_conversation_view_model_note_grouping.py`
   (projection) and, if the legacy timeline mirrors it,
   `tests/context/test_system_note_context_grouping.py` (parity).

## Resolved

- ~~Live-growing `system-note-group` snap-shut~~ — **fixed**: the group and
  solo-note renderers now read back open state via `preserveDetailsOpen`
  before `replaceChildren`, so a group that gains a member mid-turn (which
  defeats its `sameBlock` arm) no longer resets the reader's open/collapsed
  choice. Locked by
  `tests/frontend/test_frontend_conversation_surface_vite.py::test_system_note_group_preserves_user_open_state_when_growing`
  (verified by negative control: removing the read-back makes the
  growth-preservation assertion fail).

## Open questions (do not guess)

- The bilingual translation `<details>` (`:1404-1420`) is reused rather than
  replaced, but whether its open-state survives every `renderTextBlock` path
  was not exhaustively traced.
- "No `<details>`" for `attachments`, `image-generation`, `artifacts`,
  `queue-status`, `live-status`, `origin`, `context` is from targeted reading
  of each renderer, not an exhaustive line-by-line audit of every helper.
