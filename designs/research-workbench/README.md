# Tofu Research — evidence-first workbench

**Design study 01 · 2026-09-07 · proposal, not the current production contract**

The product promise: **Bring a question. Leave with a defensible next decision.**

This is a Chinese-first interaction prototype for a persistent scientific research workspace. It is deliberately isolated from the application. All papers, findings, budgets, protocols, and receipts are synthetic. There is no network access, API binding, real execution, local persistence, or scientific validation.

## Open and explore

- `index.html` + `prototype.css` + `prototype.js`: editable prototype sources.
- `research-workbench.html`: self-contained portable build; open directly in a browser.
- `previews/`: Chromium screenshots and `verification.json`.
- `verify.py`: repeatable local browser specification.

Suggested walkthrough:

1. Open **研究简报**. Read the provisional recommendation, its scope, and the next test.
2. Click a citation such as **A ↗**. Inspect the source passage, what it supports, and what it cannot support. Open the source location.
3. Open **证据与文献 → 文献列表**. Sort by year or citations using the dropdown or column-header buttons; filter verified top-main venues, workshops, or unverified publication records. Open a venue/count for its simulated provenance. **顶会范围** edits a memory-only allowlist. Switch to **证据比较**, retaining filters, sorting, and selection. Change a source checkbox to see explicit downstream revalidation.
4. Compare **H1, H2, H0** in **候选解释**. A control hypothesis is retained rather than buried below a novelty leaderboard.
5. Inspect **实验与判断**. Review the exact protocol and the separate operating budget. Open approval; it defaults denied until the explicit checkbox is checked. Approval creates only a simulated queued state, not a result.
6. Use the top **查看状态** selector: initial, approval, disconnected, interrupted, stale, completed. Selecting a gallery state resets the sample fixture; it is not an execution command.
7. Open **研究首页**. Submit another question. Only a new-question draft is shown; no sample evidence or experiment is inherited. **查看另一份完整示例** returns to the populated error-recovery example.
8. Use `Ctrl/Cmd+K`, keyboard Tab/Shift+Tab, Escape, and **收起上下文**. The inspector is nonmodal on wide screens and modal below the reading-space breakpoint.

## 1. The product decision

The old screen centers the work performed by the machine: pipeline, collection count, elapsed time, repeated running messages. The proposed screen centers the intellectual work: **question, justified belief, uncertainty, evidence, discriminating test, updated judgment**.

Research is neither a paper-library dashboard nor a long chat transcript. A project survives repeated searches and failed hypotheses. Runs are activities within that project; claims and artifacts are versioned outputs. Chat is a scoped inquiry and steering surface, not the persistence authority.

Research supports at least four intents:

| Intent | Default first deliverable | Stop when |
|---|---|---|
| Understand a field | Evidence map and bounded synthesis | The requested scope is covered well enough to inform the user's decision, or a visible resource/coverage limit is reached |
| Test a claim | Competing explanations and discriminating protocol | Evidence supports, refutes, or cannot distinguish the scoped claim |
| Reproduce a result | Reproduction protocol and dependency checklist | The result and limitations are independently assessed, or an explicit blocker is documented |
| Prepare a paper | Claim-linked outline and readiness conditions | Requested artifacts exist with current scientific dependencies; publication remains separate |

These are proposed goal modes. The prototype demonstrates selection and initial framing, not distinct implemented research engines.

### Three outcomes the first screen must make obvious

1. **What do we currently think—and how provisional is it?**
2. **What is the most important uncertainty?**
3. **What useful step happens next, and is my judgment required?**

Counts, progress, and logs support these questions. They do not substitute for them.

## 2. Mature patterns: borrow behavior, not branding

Primary-source documentation was opened during design research. These are interaction precedents, not evidence of improved scientific quality.

| Reference | Documented pattern | Adaptation | Deliberately not copied |
|---|---|---|---|
| [Elicit: create and save columns](https://support.elicit.com/en/articles/14758162-create-and-save-columns-in-elicit) | Question-relevant extraction columns; editable instructions, units, choices, exceptions, reusable presets | Suggested comparison schema with inspectable extraction rules and explicit unknown values | Chat-only access to table structure; implying identical cells make heterogeneous studies comparable |
| [NotebookLM: use chat](https://support.google.com/notebooklm/answer/16179559?hl=en) | Select sources; preview citation text; navigate to source location; preserve citations when saving a response | Visible answer scope, citation-to-context inspection, provenance-preserving promotion | Treating chat history as the scientific record or citation presence as validation |
| [NotebookLM: add/discover sources](https://support.google.com/notebooklm/answer/16215270?hl=en) | Inspect candidates before import; research can proceed without blocking other notebook work | Distinguish discovered, selected, admitted, and actually used sources; nonblocking review of proposed additions | Discarding results when leaving a view; silently treating partial coverage as complete |
| [Zotero PDF reader](https://www.zotero.org/support/pdf_reader) | Annotations become notes with citations and page links; show on page / show item | Passage → evidence note → brief/claim → manuscript, with source version retained | Requiring manual highlights for every extracted fact; relying on color alone for epistemic status |
| [Linear Inbox](https://linear.app/docs/inbox) | Priority view, contextual actions, keyboard navigation, read/unread and reminders | A decision queue containing approvals, material contradictions, blockers, and recovery judgments | Treating every tool completion as a notification that needs human attention |
| [Notion views and page opening](https://www.notion.com/help/views-filters-and-sorts) | Multiple views of shared records, side peek, focused opening, property visibility | One object, multiple projections; optional contextual inspector and full-page reading | Duplicated forms/state, a permanently cramped three-pane layout |
| [IBM Carbon data table](https://carbondesignsystem.com/components/data-table/usage/) | Tables in main content with progressive disclosure; deeper content deserves a dedicated view | Wide semantic comparison table, scope filter, row-to-inspector detail | Cramped all-purpose tables or tables used for narrative prose |
| [Microsoft Human–AI Interaction Guidelines](https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/) | Guidance covers initial use, normal operation, inevitable errors, and change over time | Explain scope, appropriate control, correction, failure, and adaptation | Assuming one ideal successful state is an adequate AI product specification |
| [W3C table pattern](https://www.w3.org/WAI/ARIA/apg/patterns/table/?x=1) | Prefer native tables when cells are not a spreadsheet-style interactive grid | Caption, headers, ordinary control tab stops, labelled horizontal-scroll region | Unnecessary ARIA grid complexity |
| [W3C status messages](https://www.w3.org/WAI/WCAG21/Understanding/status-messages.html) | Meaningful updates should be programmatically available without receiving focus | Polite result-count/scope/status announcements; no focus theft on updates | Whole-table live announcements and token-by-token progress narration |

NotebookLM retrieval caveat: the opened official URLs returned content branded “Gemini Notebook” during the research pass. The patterns above describe that returned documentation; this study does not claim to verify an older release or every subscription mode.

### Skill use and boundaries

The requested skill search found verified catalog matches for frontend design, interaction resilience, and UI/UX Pro Max. Installation of `clawhub.xobi667.ui-ux-pro-max@0.1.0` was requested and declined. **No design skill package was installed, loaded, or executed.** No substitute installation was attempted.

The existing `lib/design_sys/bibles/academic-research.md` was read as local design guidance. It is a presentation-oriented reference, not a web-application specification. The useful transfer is evidence-first ordering, disciplined page grammar, source footnotes, readable density, and anti-decoration restraint. Slide point sizes and categorical prohibitions were not blindly applied to an interactive app.

## 3. Information architecture

```text
Research home
  ├─ One question + optional outcome intent
  ├─ Continue projects, ordered by consequential attention
  └─ Decision queue

Research project (stable ID; title may change)
  ├─ Brief: understood goal → current judgment → evidence/uncertainty → next step
  ├─ Evidence: scope + comparison table + source/claim inspector
  ├─ Hypotheses: competing mechanisms, falsifiers, minimal tests
  ├─ Experiments: frozen protocol + approvals + execution + scientific judgment
  └─ Outputs: reusable brief, evidence package, protocol, manuscript readiness

Shared secondary surfaces
  ├─ Context: current decision, scope, budget, scoped questions
  ├─ Activity: factual receipts and recovery; not an alternative results view
  └─ Revision: what changed, which dependents are stale, what can be reused
```

The sidebar selects projects and project-level utilities. Tabs select research views. They no longer repeat identical destination lists. The inspector/context column can be hidden; a claim selection opens the relevant evidence. On narrow screens, evidence opens in a dialog rather than reducing the reading area to unusable width.

There is no default giant knowledge graph, novelty radar, percentage-of-science-complete, or agent org chart. A dependency view is useful on demand to explain a specific claim or stale artifact.

## 4. Visual system: quiet, precise, readable

### Direction

Use a document-centered scientific workspace with neutral archival surfaces, charcoal text, and a restrained forest-green action color. It should feel coherent while reading for an hour, not just attractive as a dashboard thumbnail.

| Token | Value / rule |
|---|---|
| Canvas | `#fafaf7` |
| Reading surface | `#ffffff` |
| Navigation chrome | `#f2f3ee` |
| Primary ink | `#242a27` |
| Secondary text | `#5d6861` |
| Tertiary text after review | `#637069` |
| Accent/action | `#345b48` |
| Provisional hypothesis | Restrained violet label with text |
| Attention | Amber; never imply a scientific loss |
| Invalid/stale | Explicit text with appropriate warning treatment |
| Borders | `#dfe4dc`, stronger `#bdc8bc` for interactive/group boundaries |
| UI radius | 4–8px; avoid nested round dashboard cards |
| Typography | System sans / Noto Sans CJK / PingFang; mono only for IDs and receipts |
| Title | 27–36px, short; full question expands below |
| Key judgment | 20–24px; primary emphasis, not marketing ornament |
| Substantive text | 14px in prototype after review; production reading mode should support 16px and zoom |
| Essential metadata | 12px minimum in the refined content surfaces |
| Minor labels | 10–11px only for nonessential prototype/chrome labels |
| Spacing | 4/8/12/16/24/32px rhythm |
| Motion | No looping celebration or progress theater; reduced-motion supported |

Initial contrast calculations found muted text/canvas ≈5.55:1 and white/action ≈7.68:1. The initial tertiary token was below 4.5:1 and was darkened. This is not a complete WCAG audit.

### Layout budgets

- Compact header, then immediately accessible sticky navigation.
- Constrain the wide-screen content area; narrative should not stretch to 1180px.
- Main document remains dominant. Evidence tables may horizontally scroll within a labelled region, never force the whole page wide.
- Context moves below the document at ≤1100px; evidence inspection becomes modal there.
- At 390px use 18px margins and an early compact decision action before the long findings section.
- Collapse routine recent activity by default. It must not make the side panel hundreds of pixels taller than the brief.
- Citation targets are 28px minimum on desktop and 32px on mobile in this prototype; production can adopt 44px targets where context permits.
- Visible focus, logical dialog focus, and preserved selection are part of the interaction design, not a finishing pass.

## 5. Screen and interaction contracts

### Research home

One meaningful question is enough to begin. The outcome chips refine the desired result, not the workflow machinery. Optional seed sources, files, and project context are a future integration; the prototype does not offer fake upload controls.

Production should show an immediately editable interpretation of goal, scope, budget, and expected first deliverable. Ask only clarifications whose answers materially change the work. A nonblocking proposed scope is not user authorization for external computation.

### Living brief

Ordering:

1. Short question/title and expandable exact original request.
2. Scope and hard constraints.
3. Provisional or reviewed judgment with clearly scoped status.
4. Evidence-backed findings and significant opposing evidence.
5. Smallest justified next step.

Mobile adds an early decision shortcut. The full rationale stays in the document. Accepted content must not reorder while someone reads; production should use the existing stable DOM owner and an explicit “new findings available” control for disruptive changes.

The initial state contains an understood goal and planned comparison—not synthetic findings. For a newly typed question in this prototype, downstream views stay empty until explicitly switching to the separate populated example.

### Evidence and source inspection

Each evidence object carries the source identity/version, exact locator, quotation, extracted fact, interpretation, and limitations. They are not one undifferentiated paragraph.

A citation opens an inspector with:

- Which object is selected.
- The exact passage or honest unavailability state.
- Source location and version.
- What the evidence supports.
- What it does not establish.
- A return path to the original view.

Source selection changes are drafts. The old brief remains tied to its old snapshot. Removing a source does not instantly produce a newly valid brief. The prototype marks dependent use as requiring revalidation and blocks reuse of old approval.


### Literature list: comparison density without paper cards

The evidence page now defaults to a full-width **native table**, with the optional context column closed until needed. No separate boxed paper cards. Two projections share the same four source objects:

- **文献列表**: selection, paper/question relation, year, venue/acceptance, citations, reading status.
- **证据比较**: selection, paper/question relation, budget comparability, original-text coverage, limitations.

Year and citation columns have real button controls; only the sorted column receives `aria-sort`. The dropdown offers relevance, year ascending/descending, and citations ascending/descending. This follows the verified [W3C sortable-table example](https://www.w3.org/WAI/ARIA/apg/patterns/table/examples/sortable-table/), not an unnecessary spreadsheet grid. Enter/Space activation, visible direction, retained focus, and polite count announcements are implemented. Default relevance order is an explicit **synthetic fixture rank**, not a model-calibrated score. Numeric fields sort numerically; missing values remain last in either direction; ties use Chinese-locale title then stable source ID. A known citation count of `0` is not a missing `—`.

Search trims whitespace and matches title, research scope, limitation, venue and year case-insensitively. Venue, role, and search filters compose. Filtering, sorting and switching projections never alter source admission or invalidate the brief. Only explicit source selection does that. View/filter/sort choices survive tab navigation in this memory-only sample; resetting the gallery clears them. Production updates must not reorder a list while it is being inspected; apply a new metadata snapshot explicitly.

The desktop table pins selection/title within its own scroll region. At ≤640px it keeps semantic table structure and local horizontal scrolling **without pinned columns obscuring other cells**; a full-width sort selector remains above the table. Any title opens a source dialog on narrow screens, including year/venue/count and the provenance action. There is no document-level horizontal overflow in the checked viewport sizes.

#### Bibliographic facts are not scientific evidence

Every fixture and provenance panel is explicitly **synthetic, not live verified**. The offline sample deliberately has no invented external evidence links.

| Structured fixture field | Meaning / production requirement |
|---|---|
| `publicationYear: number \| null` | Formal publication year when known; otherwise preprint first-publication year with a visible basis. Never storage/import year. |
| `yearKind`, `preprintYear` | Preserve the displayed-year basis and preprint year separately; venue edition also needs its own field in production. |
| `citationCount: number \| null` | Nonnegative count from one provider snapshot. Never add counts across databases or across linked versions. |
| `citationProvider`, `citationCheckedAt` | Provider and retrieval timestamp of this count, not merely the source-record modification date. |
| `publicationStatus` | `accepted`, `not_found`, or `unknown` in the fixture. Absence from a lookup is not rejection. Production needs explicit pending/error/conflict states and independently evidenced rejected/withdrawn states if offered. |
| `venue`, `track`, `venueCheckedAt`, `venueEvidence` | A specific venue/edition/track match plus official decision or authoritative accepted-paper/proceedings evidence. A submission URL, venue text, arXiv comment or inferred title alone cannot establish acceptance. |
| `metadataRecord` | Fixture snapshot ID. Production stores exact source IDs, matched identifiers, provenance links and immutable observation/version references through existing owners. |
| `relevanceRank` | Explicit demo ordering only. Relevance and admission require a separate validated production policy. |

Top-tier is a **configurable scope**, not a universal official ranking or a quality score. The demo preset is ICLR/ICML/NeurIPS main conferences. **顶会范围** offers eight illustrative venues to toggle for the current session. Membership alone is insufficient: verified acceptance and `track=main` are required. The workshop fixture retains its parent venue name but never acquires a main-conference badge. A preprint says **未发现录用记录 · 不代表拒稿**; an unmatched record says **待核验**. Identity, full-text reading, publication metadata and scientific support remain independent states.

Production should match DOI/provider/arXiv identifiers and link preprint/published manifestations only with adequate bibliographic evidence, never title-only merging. Preserve conflicting observations instead of silently selecting the most prestigious venue or largest count. Reference extraction must be structured, source-backed and refreshable—not inferred by a browser or free-form LLM text. Provider throttling, bounded response sizes, cancellation and cache expiry belong in the existing backend adapters. Missing/failed metadata must not block usable evidence or prompt the user to do routine lookup work.

### Hypotheses

Keep the null/competing explanation visible. Compare mechanism, prior-work difference, support, counterevidence, falsifier, and cheapest discriminating test. Use a table for high-dimensional comparisons; use concise briefs for mechanism explanation.

Do not headline model-authored novelty scores or estimated success probabilities that have not been calibrated. A justified recommendation can still be useful without fake precision.

### Experiments and approval

Prepare the protocol automatically, but approve the frozen version. The dialog specifies protocol snapshot, executing capability, target, effects, resource ceiling, stop conditions, and scope-change consequences.

Important distinction:

- Research operating budget: what conducting the investigation may spend.
- Experimental fairness budget: identical constraints used to compare scientific interventions.

Approval cannot relax experimental fairness. A proposed next test cannot authorize itself. A queued job is not a completed job. A completed tool call is not evidence that a hypothesis is supported.

Approval dialog content changes explicitly move focus to the new heading. Focus is trapped within modal dialogs and restored on dismissal. Desktop evidence inspection is nonmodal and Escapable; closing returns to the source trigger.

### Outputs

Offer the output needed now: decision brief, source-backed evidence package, protocol, negative result, reproduction checklist, or manuscript.

Manuscript readiness is a conjunction of inspectable conditions: current protocol, artifact-backed runs, current supported/scoped claims, complete source, and matching compile receipt. Do not replace this with a readiness percentage. A PDF that compiles is not a scientific result.

## 6. State semantics

| Situation | Show | Do not do |
|---|---|---|
| Just started | Goal, assumptions, first planned deliverable, actual receipts | Fill the brief with imagined findings |
| Collecting/synthesizing | Acknowledged source/semantic outputs, coverage limits | Call metadata “read”; make heartbeat time into progress |
| Needs approval | Exact proposed action, cost/effects, approve/edit/defer | Treat an action suggestion as authority |
| Approved | Saved scope/operation receipt, queued/running execution separately | Call it a successful experiment |
| Disconnected | Last confirmed snapshot and connection problem | Infer that the job stopped or restart it automatically |
| Interrupted | Saved checkpoint, repeatable remainder, external-effect check | Claim rollback or blindly submit remote work again |
| Source/protocol changes | Previous version, impact scope, invalidated dependencies | Continue labeling old claims current |
| Completed delivery | Available artifact set and remaining unknowns | Claim scientific success or paper readiness without evidence |
| Negative/inconclusive science | Scope, evidence, uncertainty, recommended stop/refinement | Reward novelty or positive outcomes at the expense of validity |

Production should keep execution, connectivity, scientific outcome, validity, and draft-save state separate. The prototype uses a resettable gallery to demonstrate them; it is not a new runtime model.

## 7. Architectural implementation sequence

The prior source audit found substantial current infrastructure: an independent lazy research entry, task-addressed durable records, owner-scoped workspace CAS, bounded progress, evidence digests, hypothesis/protocol/claim/manuscript checks, exact capability bindings, and generic task execution.

Preserve that authority. Do not create a new research database, polling transport, durable event log, permissions system, or agent engine.

### P0 — Product identity and honest projection

1. Extend the existing research contract with a stable owner-scoped project ID independent of title/direction, language, or task run. Retain originating run and candidate-version references.
2. Define a versioned brief projection from acknowledged artifacts: understood goal, scoped judgments, evidence links, known limitations, next decisions, dependency status.
3. Keep raw streamed prose provisional. Stage checkpoint acknowledgement and repository artifact publication remain distinct.
4. Bind the selected discovery run/corpus snapshot through experiment and writing context; do not silently fetch the latest artifact with matching wording.
5. Move the existing UI content into this shell using normal TypeScript modules and the current style/manifests. Share one workspace revision owner. Do not copy this standalone JavaScript into the retained runtime.

### P0 — Source quality and bibliographic enrichment (not implemented in production)

Read-only repository inspection for this refinement found:

- `contracts/research_record_v1.schema.json` paper rows are a closed projection with IDs/title/status/error/page/text/degraded fields. They do not carry year, citations or venue. `lib/research/records.py::persist_record` maps that same set from `paper_done`; adapter changes alone will not reach the UI.
- `lib/research/literature.py::AcademicDiscovery.semantic_record` already extracts year, but `lib/research/discovery.py::discover_corpus` drops it in canonical merged rows. Current Semantic Scholar requests ask for title, abstract, year and external IDs, not counts or venues. `crossref_record` currently extracts DOI/title/abstract.
- `lib/research/recipe.py::_run_harvest` passes arXiv IDs/title hints, not enriched metadata. `lib/paper/library.py::_PAPER_LIB_COLUMNS` has no year/count/venue. Library timestamps are not publication years. Manuscript `venue` in the research-program schema is a writing destination, not source-paper acceptance.
- Discovery ranks lexical overlap and marks 12 core candidates, but does not enforce a relevance admission threshold before harvesting arXiv IDs. This is a separate source-quality gap; sorting cannot repair it. Production needs relevance review with inspectable inclusion/exclusion reasons and preserved uncertain candidates, rather than a simplistic title-only veto.

The future vertical slice is **contract → existing discovery adapters and canonical identity merge → harvest/event metadata → durable record projection → typed literature view**. Do not add frontend-direct catalogue requests, a second bibliographic database or a new polling owner. Preserve explicit owner IDs, acknowledged Sidecar publication, the 200-paper/8-MiB record ceilings, cancellation, fixed catalogue authorities and existing request/response budgets. Metadata enrichment and refresh must have bounded requests, work and cache lifecycles; total citation graph edges are not a citation-count metric. Production rollout needs adapter and projection tests with missing, conflicting, rate-limited and duplicate-version records.

API field candidates include Semantic Scholar `year`/`citationCount`/publication venue and OpenAlex publication/cited-by fields. This pass did **not** freshly verify their complete remote schemas: API docs rendered incomplete/moved pages, and the attempted OpenReview guide was unavailable. No new provider integration contract or live acceptance inference is claimed here. The source panel's Semantic Scholar numbers are local fixtures only.

### P0 — Resource policy and decisions

1. Add machine-readable outcome/constraint/operating-budget intent before new enforcement behavior.
2. Reuse launch-probed bounded workers, queues, model routing, exact tool bindings, and user ownership boundaries.
3. Persist a specific approval/operation record before external dispatch. Record remote operation IDs and receipts even when later workspace CAS application conflicts.
4. Keep explicit experiment/analysis/compile/publish approval boundaries until an appropriately scoped policy replaces them.

### P1 — Selective revision and reusable science

1. Connect existing IDs/digests across corpus → evidence → gap → hypothesis → protocol → run → claim → manuscript section.
2. Explain which dependency changed and why it matters.
3. Reuse unchanged sources, extraction, and protocol material; rerun affected work only.
4. Preserve rejected hypotheses and scoped negative results as reusable knowledge, not failed UI items.
5. Add a three-way conflict view for unsaved edits versus a newer workspace revision. Never overwrite acknowledged server state with a browser draft.

### Migration and release discipline

Start with the current owner maps in `docs/README.md`, `docs/RESEARCH_CYCLE.md`, and `frontend/src/features/research/AGENTS.md`. Machine-readable contracts change first; generated consumers follow. Ship a vertical slice—brief → one evidence → one exact decision—before expanding the entire schema surface. Update the production authority documents only when the behavior actually lands.

## 8. Evaluation: best result with least effort

Do not optimize for clicks alone. Optimize for **independently defensible decision quality per unit of user effort and operating budget**.

Usability tasks:

- State the current conclusion and its biggest uncertainty within 30 seconds.
- Find the exact evidence for a material statement in one selection plus one source-opening action.
- Identify a metadata-only source without opening logs.
- Predict what an approval will do and what it will not do before clicking.
- Recover from disconnection without a duplicate execution.
- Explain why a changed source makes a claim stale.
- Deliver a negative or inconclusive result without being pushed into an unjustified experiment.

Measure time to first useful source-backed artifact, comprehension, evidence-verification effort, clarification count, approval comprehension, duplicate-execution rate, recovery success, and independently audited research quality under matched budgets. These are proposed metrics; no improvement is claimed from this mockup.

## 9. Verification and known boundaries

Run:

```bash
node --check designs/research-workbench/prototype.js
python3 designs/research-workbench/verify.py
```

The hermetic Chromium test blocks all requests and does not import or start the application. It covers six views at 390/768/1024/1440/1920px, seven fixture states at mobile/desktop, source filtering, changed-scope invalidation, approval from the waiting state, simulated queued receipts, decision deferral, dialog keyboard confinement, focus on nested-dialog replacement, optional context, initial-project isolation, inline input errors, and escaped question text.

`previews/verification.json` records the concrete result. Screenshots are generated for human review.
 The added literature specification covers numeric year/count orders, deterministic ties, null/zero separation, header keyboard focus and `aria-sort`, combined filters, workshop/main exclusion, editable top-venue scope, provenance timestamps, shared projections, local mobile scrolling, and opening citation/title details. The portable build is generated by `python3 designs/research-workbench/package.py`; use `research-workbench.html#evidence` to open the literature view directly.

Limitations:

- Screenshot tools returned encoded image payloads rather than visible images to the design agent. Layout was checked through real-browser geometry, computed styles, overflow assertions, and interactions—not a complete human visual review.
- No production build, server restart, deployment, live data mutation, external task, or model call is part of this prototype.
- No scientific results, efficiency gains, full-text coverage, calibrated confidence, or publication success are established.
- The sample remains one research question. Its initial-input screen demonstrates a generic draft, not an implemented planner.
- Opening a new question does not save it; refresh clears transient interaction state. Only route/gallery state is encoded in the hash.
- Exact visual taste, screen-reader experience, 200-source performance, 200% text scaling, and actual user comprehension still require production-stage and human validation.
- Raw CSS/JS are intentionally standalone prototype assets, not a replacement for the project's typed owners and canonical contracts. On implementation, migrate behavior into existing frontend owners and retire the proposal artifact when no longer useful.
