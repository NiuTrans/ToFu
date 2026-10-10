# Automatic context injections

Owner: `lib/tasks_pkg/context_composer/`. Domain map: [context engineering](modules/context_engineering.md). The executable inventory is `registry.py`; this contract describes its current semantics, not a point-in-time audit archive.

## Unified interface

Every engine-authored conversational context addition uses the public `lib.tasks_pkg.context_composer` interface. Register its `InjectionSpec` first: stable ID, source, necessity, actual trigger, required/optional status, authority, lifecycle, lane, priority and content-token ceiling. Unknown production IDs fail closed. Providers acquire owner-authorized evidence; they cannot invent placement, priority or budget policy. Registration never grants tool/filesystem permissions.

| Lane | Interface and invariant |
|---|---|
| Snapshot | `context_block(id, content, ...)` then `append_context_blocks`, or ordinary `compose_task_context`. Content-addressed reuse preserves bytes. Registered `platform_static` and `ui_nav_directory` occupy dedicated system-prefix carriers (one per block, authority-ordered); other blocks stay tail user carriers and verified withdrawal appends one tombstone. A system carrier is withdrawn outright on authoritative absence (replace mode, explicit ablation, non-browser surface); transient acquisition failure retains the previous version with an explicit decision receipt. **Tail amalgamation:** blocks are sorted by `(authority, priority, id)`, so same-authority blocks are adjacent; each maximal same-authority run being appended in one pass folds into ONE `<system-reminder>` (`[Context authority: X; N context blocks]`) whose sections keep their own `tofu-context:<id>` markers. Reuse stays per-block granular — `_managed_block_hashes` hashes the wrapper-independent body section (`_canonical_section`), so a changed sibling re-appends alone while unchanged group-mates are reused verbatim; a lone block keeps its standalone envelope byte-identical. |
| Event/inbox | `append_injection(messages, id, text, task=..., round_num=..., reason=...)`. Preserve payload and chronological identity, including identical retries after compaction. A receipt UUID travels with the carrier. No hand-appended guidance dictionaries, historical-message mutation or duplicate `record_injected_note`; the writer owns note projection for registered recovery events. Non-appended diagnostic/error notes remain segment-owned. |
| Bootstrap contract | Only registered `worker_policy` may append a system contract, before any dialogue. Runtime events cannot choose a role or elevate authority. Swarm/goal-mode workers are first-class conversational agents: after the objective they go through the same `compose_task_context` boundary as the root agent with the parent's resolved config (same ambient blocks, feature gates and manifest under the child's Inspector ID). A bare `system` carrier is emitted only when the inherited prompt is non-empty — with no inherited prompt `worker_policy` opens the request, so strict providers never see an empty position-0 system message. Checkpoint recovery refreshes every registered injection ID (worker policies and ambient blocks) from the fresh bootstrap before retained dialogue, preserving read-only/tool scope at its original authority and dialogue byte-for-byte. |
| Telemetry | `record_injection_diagnostic` accepts numeric/boolean metrics only. `context_budget` cannot append a message or intervention card. |

Chat Goal workers are continuing conversation agents, not delegated bootstrap
workers: they retain the actual model working set, use the ordinary composer,
and append only Goal-role guidance and synthetic review/directive events. The
separate virtual-user reviewer still uses isolated bootstrap inputs. See
[TASK_EXECUTION_MODES.md](TASK_EXECUTION_MODES.md).

`_policy_prefix.py` owns the static exception: insert after explicit system/developer policy and before dialogue, reuse unchanged bytes, and replace only that engine policy on a genuine profile change. Replace mode with explicit operator policy removes it. Each registered system-placement block owns exactly one dedicated carrier, re-seated in authority order only on a genuine membership/content change; a non-system-role witness never satisfies its reuse. Only `platform_static` top-level legacy envelopes matching producer-recorded hashes migrate once from mixed user carriers; nested project/memory quotes and all other bytes stay unchanged. Missing/mismatched provenance never promotes content or suppresses fresh native policy; no user/tool XML is interpreted as policy. Old archived requests are never rewritten. This prefix migration/change may invalidate one upstream cache prefix; subsequent unchanged rounds are stable. Static prefix insertion cannot split a tool batch; tail blocks still defer.

Both snapshots and events enforce closed tool-call/result batches. Snapshots defer; inbox producers check `can_append_injection` **before draining**. Human-steer classification and producer-owned delivery ACK remain unchanged. Appended never means consumed or acknowledged.

`systemPromptBlocks.disabled` suppresses optional **registry IDs** through the shared interface. Required rules, accepted checklist state, explicit user branch input, human/background/peer delivery and evidence-triggered safety/recovery cannot be suppressed by advisory ablations. Their explicit product modes/permission controls retain their existing owners. Budget diagnostics are not an opt-in model notice.

**Private-input discovery — Problem:** A model-visible snapshot directory adds changing context, but availability does not establish adoption or a correct, cheaper answer.
**Decision:** `context.programmaticInputs` is a request-local boolean experiment, default false; invalid values fail closed (strict validation raises). Explicit true still respects ownership, live catalog and `systemPromptBlocks.disabled`. The callable `tools.inputs()` and `tools.result({ref})` remain available independently.
**Alternatives considered:** Default-on discovery lacks matched quality/cost evidence; removing the helpers would discard the independently verified exact snapshot boundary. Promoting the directory requires natural reuse and matched answer correctness before cost or latency comparisons.

Context acquisition never reads JOURNAL history, seeds/rotates a journal, writes `.gitignore`, or invents journal-maintenance instructions. Project/user instructions own maintenance; the prompt supplies only a primary-root locator and the historical-evidence boundary.

Protocol output (assistant messages, tool calls/results), explicit user attachments/quotes, assistant prefill, compaction summaries and separate workflow requests have their own owners. They are not advisory prompt injections. A new exception requires an executable regression and an update to this boundary.

## Freshness, relevance, and evidence integrity (decision 2026-09-30)

**Problem:** Append-only snapshots could leave contradictory current guidance; quoted or injected text could select unrelated memories; failed acquisition or recovered logs could be mistaken for authoritative absence or successful work.

**Decision:** Snapshot identity is registry-owned and content-addressed. A changed tail snapshot explicitly supersedes the previous version; a verified withdrawal appends a tombstone. Provider failure, partial project-rule acquisition (including per-file read/stat failure), skipped prefetch, and missing selection status retain the last complete snapshot. A successful empty memory selection is a silent retrieval decision, not invalidation of earlier knowledge; only explicit disabling withdraws that evidence block. Registry-owned `silent_absence_reasons` record no-match and absent project-page decisions with zero appended tokens and no timeline note. Verified rule-file removal may replace old rules. Compaction/replay rebuild snapshot state from owned markers and cannot reactivate superseded content.

Memory relevance uses separately preserved human-authored text, never quoted replies, attachments, helper instructions, injected carriers, or synthetic turns. An empty/image-only latest request does not inherit an older request. If a prompt hook rewrites a reference-bearing projection, relevance abstains rather than reuse unredacted provenance or mistake the rewritten references for authored text. Pre-compaction observer hooks receive a detached snapshot of messages and task, so hook mutation cannot alter live context.

Only platform-added envelopes provide platform attribution; matching tags inside user or tool data do not establish provenance. Full-profile instructions distinguish exact delays from completion-driven waits, describe count-and-token-bounded result retention, and name only current authorized tools. The navigation directory derives labels from DOM ownership, with an independent semantic inventory and fail-closed missing/ambiguous associations (see `docs/RENDER_CONTRACT.md`).

Background controls and receipts follow the live executable catalog, not stale role or wire snapshots; denied controls are never restored. Swarm recovery preserves failed/interrupted/unknown evidence as partial output, requires owner/conversation/tenant scope, and does not make recovered handles cancellable. See `docs/modules/tools_execution.md` and `docs/RETAINED_AGENTS.md`.

**Alternatives rejected:** Deleting archived dialogue destroys evidence; treating every empty provider result as a withdrawal confuses failure with policy; relevance from full projected prompts launders references; unconditional control reinsertion widens authority; recovered prose is not a terminal success receipt.

Executable specifications: `tests/context/test_context_integrity.py`, `test_context_refresh_failures.py`, `test_full_prompt_integrity.py`, `tests/tasks_pkg/test_background_guidance_authority.py`, and the lifecycle/recovery suites named in the owning domain maps.

**Necessity — Problem:** Retrieval misses were presented as knowledge invalidation; an untracked trusted date caused duplicate clock facts; global work feeds distracted ordinary workers and overlapped planned coordination.
**Decision:** Keep no-match receipts off the model stream, recognize actual trusted clock bytes, and gate optional project pages with the existing coordinator binding before acquiring a cursor. Suppressed, deferred or truncated pages cannot advance the cursor. Existing scoped inbox and file-overlap delivery remain unchanged.
**Alternatives rejected:** Merely hiding cards still spends prompt tokens; a task watermark cannot prove visibility after compaction; text/LLM relevance filters or a second coordination state machine would duplicate authority. Tests: `tests/context/test_context_necessity.py`, `tests/project_mod/test_project_brain_signal_driven.py`.

## Intervention reason presentation

The human-facing rendering of an intervention's **reason** is owned by the registry, never hand-duplicated in the frontend. A spec whose lane surfaces a model-visible timeline note may declare `InjectionSpec.presentation = NotePresentation(label_key, tone, fallback)`: `label_key` is a `systemNote.reason*` i18n contract key, `tone` is one of the closed `NOTE_TONES` (`ok`/`warn`/`err`/`info`) that selects the chip colour, `fallback` is the port-less English string. Declarations are validated at import (tone membership + key shape).

Both conversation render surfaces **derive** this vocabulary from the registry rather than maintaining their own maps:

- `scripts/gen_system_note_presentation.mjs` reads `registry.note_presentations()` and emits the typed contract `frontend/src/conversation/ui/system-note-presentation.generated.ts`; the typed renderer imports it.
- The retained JS runtime (`tool_rounds.js`) cannot import the TS contract, so it embeds the same map between `BEGIN/END GENERATED` markers; `scripts/check_note_presentation_parity.mjs` fails if the embed drifts.
- `tests/context/test_context_injection_presentation.py` pins every registry `label_key` to both locales and every `tone` to a `.sw-note-reason-{tone}` stylesheet rule.

Run `npm run generate:note-presentation` after editing a presentation; `npm run check:note-presentation` (contract staleness + parity) gates it. Reasons absent from the registry (a newer backend, or a snapshot block with no timeline note) render the neutral `info` chip with the raw reason id, preserving the closed-world fallback. Adding a new intervention reason therefore requires exactly one registry edit plus `generate` — no frontend map edits.

## Debugging and resource bounds

Open a captured round in **Request Inspector → CONTEXT**. `contextManifest` covers initial providers, runtime recovery/inbox and child-agent injections. Paper engines retain the same manifest on their own task. Worker manifests stay under the child's Inspector ID, never the parent's mutable log.

Rows expose `registryId`, `source`, `necessity`, `trigger`, `required`, `lane`, `placement`, `maxTokens`, `hash`, `reason`, `chars`, `tokens`, `appendedTokens`, and event ID/round where applicable. `tokens` uses the shared model counter with its ordinary fallback; `appendedTokens=0` distinguishes reuse/suppression from new cost. Telemetry always has `injected=false`, `appended=false`, `tokens=0`, `placement=telemetry`.

Receipts describe append/decision history, **not** a sum of current provider input or proof of consumption. Original append receipts are immutable; one latest non-appended decision per ID exposes provider failure/recovery without per-round accumulation. History is task/child-scoped and capped at **128 rows and 128 KiB**; `_contextManifestDropped` counts oldest-row eviction. Receipts contain no prompt/result bodies or vault values; source-defined bounded provenance can contain paths/names.

The toolbar measures last prompt usage / **full** model window, falling back to post-compaction estimates when necessary. Budget diagnostics measure current working-message estimates / **usable** input after reserves. Their numerator, denominator and observation time differ: a screenshot cannot establish simultaneous measurements. Neither is a task deadline. Automatic compaction and final-wire admission enforce capacity without model-facing countdowns.

## Per-injection necessity and admission inventory

`INJECTION_REGISTRY` is the executable source of IDs/ceilings. Block ceilings bound **content**; wrappers are counted separately. Required blocks cannot be displaced by optional global budgets. Event payload bounds/retry caps remain with their evidence-producing owners; the writer does not silently clip human input or execution results.

| Registry ID | Necessity decision and admission | Content-cost policy |
|---|---|---|
| `platform_static` | Keep platform operating contract as dedicated system prefix; absent in explicit replace-system mode | Required; no advisory clipping; not lossy history |
| `role_directive` | Keep only for an explicitly specialized role | Required; 5,000 tokens |
| `goal_objective` | Chat Goal worker's durable objective restated verbatim (inline bound 4,000 chars; byte-stable per run so prefix caching holds and compaction cannot drift scope) | Required; 8,000 tokens |
| `project_rules` | Keep applicable AGENTS/CLAUDE/etc. rules in full; JOURNAL is locator-only | Required; no advisory clipping |
| `user_context` | Optional enabled, nonempty preferences; not task evidence | 1,000 tokens |
| `memory_guidance` | Short evidence/retention boundary when memory and tools are enabled; no duplicate CRUD tutorial | 180 tokens |
| `skills_index` | Discovery index only with admitted `load_skill` in the frozen wire or executable catalog; task-frozen; bodies on demand; capability removal suppresses even a cached index | 1,400 tokens |
| `ui_nav_directory` | Browser-frontend conversation turns only (Conversation Sync v3 turn authority; `non_browser_surface` otherwise); static contract-generated directory of valid `setting:`/`page:` jump keys; never hand-maintained; degraded silently when the contract is absent; dedicated system carrier, withdrawn off-surface | 1,400 tokens |
| `selected_skills` | Exact `$skill-id` references from current user text; owner-resolved instruction pages and content-hash receipts | 8,000 tokens; at most three initial pages, 6,500-token acquisition target |
| `credential_vault` | Nonempty name-to-variable index only with admitted `run_command` in the frozen wire or executable catalog; never values or permission to use every entry | 1,000 tokens |
| `parallel_execution` | Available spawn lane; current schema owns role/tool scope. Waiting guidance uses explicit returned handles with `poll(wait_s=...)`, never retired discovery names or no-ID dashboard polling | 128 tokens |
| `programmatic_execution` | Enabled mode and a wire/deferred code action or admission-compiled execution gateway; use the actual exposed action | 128 tokens |
| `programmatic_inputs` | Explicit `context.programmaticInputs=true` experiment (default false), owner-bound captures and admitted program action; versioned references, no arguments or source bodies; failed acquisition or opt-out withdraws stale snapshots | 256 content tokens, 4 entries, 4,096 bytes; omit whole block under global budget pressure |
| `project_context` | Only the exact available owner/project-bound coordinator receives unseen Project Brain narrative and overview; ordinary workers use scoped inbox/overlap notices, never the global feed. Absent pages, disabling and acquisition failures stay diagnostic-only. Only a complete admitted/reused page may await provider-success cursor ACK | Optional; 1,800 tokens; omit whole page under block/global budget pressure |
| `active_checklist` | Restore only when no complete current accepted snapshot survives: a paired direct/gateway todo result (including load/lookup) or owned recovery carrier proves visibility, without any hash in its prose. Keep at most one current recovery carrier; preserve real dialogue and audit history. Compaction carries state transactionally inside its own projection. See [continuity contract](CONTEXT_COMPACTION.md#authoritative-checklist-restoration) | Required; no advisory clipping |
| `task_state_v1` | Required projection only with explicit global-context-plan feature | 2,400 tokens |
| `relevant_memories` | Optional high-confidence owner-scoped retrieval from the latest authored request; evidence, not instructions. No match adds no prompt/timeline notice and does not invalidate historical evidence. Explicit disabling withdraws the block; provider failure retains last-known evidence with a diagnostic receipt, never claiming a fresh empty selection | 1,500 tokens; zero appended tokens for no match |
| `plan_mode` | Contract only while explicitly enabled | Required; 900 tokens |
| `environment` | Current routing facts and authenticated owner browser-extension connection evidence; multi-root/remote warnings only when applicable; explicit environment-off mode respected | Required when enabled; 400 tokens |
| `project_path_change` | One warning for an observed root move; never first sight/steady state | Required; 160 tokens |
| `mcp_tools_delta` | Reachable names absent from frozen wire, with discovery/execution available; no partial schemas. Under `mcpToolExposure='delegate'` it renders the WHOLE connected catalog (wire is empty by construction, no freeze state written) and adds spawn_agents delegation guidance | 700 tokens; at most 8 names + 80-character hints |
| `disclosed_tools` | Deferred name/signature handles from accepted disclosures; retract removal/gateway loss | 700 tokens; at most 16 names |
| `current_date` | Reuse the latest complete owned clock section or top-level system/developer date fact still in the working input. Reintroduce after loss/compaction or a day change; user/tool quotes, XML excerpts and fenced examples are not witnesses. No independent watermark/cache | 80 tokens; zero appended tokens when present |
| `modified_files` | Only files changed OUTSIDE this conversation (other-conversation/unattributed journal records stamped at/after task start); own-conv writes excluded; fires the round after detection, never repeats an unchanged file set (task-held signature watermark); journal poll throttled to once per few seconds of task time | 1,600 tokens; at most 5 paths |
| `tool_progress` | Corrections for observed no-ops/redundant reads/unresolved evidence, not productive serial work | Required; breaker-owned caps |
| `execution_allowance` | Finite invocation allowance after pre-notice full-prompt measurement and before final admission: input/output headroom after tree/local consumption and live reservations, effective tool slots and earliest deadline. Reports an input-only comparable-request upper bound excluding output/notice/growth/provider adaptation; 6/3/1 settled-headroom request milestones complement 50/20/5-percent milestones. Required archive/review handoffs remain original checks; never waives original checks or role permissions | Required; final prompt is remeasured after an appended notice; 350 tokens per notice, compact complete facts when needed; if still over the ceiling, explicitly defer in Inspector without advancing the watermark; at most four finite milestones plus one unknown-usage observation per invocation |
| `tool_stagnation` | Repeated identical outcomes, not elapsed time or call count | Required; breaker-owned caps |
| `tool_repeat_reminder` | Early exact-repeat notice one rung below the corrective nudge; never vetoes, audits on its own ledger, cleared by a real human steer | Required; once per repeat digest |
| `partial_stream_retry` | Partial transport cut without assistant-prefill support | Required; recovery-owned caps |
| `terminal_recovery` | Empty terminal output or unexecuted packet | Required; at most two retries |
| `intermediate_response` | Provider-marked commentary-only output, not every short answer | Required; bounded retries |
| `todo_incomplete` | Historical receipts only; new execution never emits checklist-only continuations | Read compatibility, not a live completion gate |
| `intent_stall` | Retryable tool failure then prose-only stop, without human handoff | Required; once per task |
| `fallback_synthesis` | Fetched evidence exists but final answer is missing | Required; fallback-owner policy |
| `evolution_guidance` | Bound executor's plan and owner-scoped learned strategies | Optional; 800 tokens; excluded from coordinator/reviewer |
| `evolution_updates` | Current goal updates at a closed tool boundary | Required; eight events / approximately 8,000 characters; presentation is separate from acknowledgement |
| `inbox_delivery` | Real human steering, swarm results, peers, background completions, overlap advisories at safe boundary | Required; bounded queues; payload/ACK preserved |
| `url_prefetch` | Optional successful retrieval of user-provided URL, clearly evidence | Fetch-owner budget; payload unchanged |
| `lifecycle_recovery` | No-refire warning after interrupted lifecycle command with unknown outcome | Required safety; recovery-owner bound |
| `branch_context` | Explicit branch title/selection without rewriting original request | Required user projection |
| `paper_tool_guidance` | Short hint only when both discovery/execution gateways exposed | One hint per Paper message view |
| `worker_policy` | Child role/scope/tool/read-only/shared-workspace contract at system authority | Required bootstrap; frozen per worker/revalidated on resume |
| `worker_artifacts` | Optional shared-artifact discovery; names only, not bodies | At most 20 short keys; 1,600-character key hint |
| `worker_dependencies` | Completed explicit prerequisite evidence, separate from delegated user context | Required; scheduler-owned evidence bound |
| `worker_wrapup` | One tool-less honest partial report after halt without final answer | Required; one wrap-up attempt |
| `goal_worker_directive` | Pending engine control for the continuing Goal worker; not replayed handoff text | Required; finite graph budget |
| `goal_worker_review` | Full latest completed VU review appended once as synthetic evidence, never human authorization; no reviewer tool trace | Required; ordinary working-set compaction |
| `virtual_user_directive` | Pinned objective and simulated-user role for explicit autopilot decision turn | Required; existing VU version/classification preserved |
| `context_budget` | **Remove from model context**: engine-owned capacity offers no useful model action | Telemetry only; zero prompt tokens/cards |

## Conversation visibility invariant

Every addition to the model-visible message stream leaves a display-only
`system_note` segment in the Turn Blocks timeline, so nothing is injected
silently. Emission is **note-after-commit**: the composer append boundaries
(`append_context_blocks` for round-scoped blocks, `compose_context` for the
session prefix) read the render manifest and record a note only for rows whose
bytes were physically appended (`appended=True`). Content-addressed reuse
reports `appended=False`, so steady-state turns stay quiet and a changed block
re-fires exactly once — never while pointing at absent content.

For accepted checklist recovery, a changed state alone is not a trigger if its
complete result remains visible. Its standalone carrier is exempt from tail
amalgamation so superseded recovery can be removed without rewriting siblings.
Notes/logs use the loop's `_activeRoundNum`, not the number of tool entries;
one model round may have many tools or none. Preserved progress explanations
are explicitly attributed to the accepted todo update, not to system recovery.

| Note kind | When emitted | Body |
|---|---|---|
| `context-injection` | A round-scoped block commits mid-turn (`modified_files`, `current_date`, …) or a registered event carrier reaches the model | The injection's own human-readable text (`noteText`) — the `except`-clipped rendering, exactly what the model sees |
| `session-context` | A conversation-level static block commits its system prefix | One fire-once aggregate listing the injected block IDs; re-fires only if the committed set/hash changes after a reload |

The durable entry carries `noteKind` (+ `injectionId` for event lanes) and
`reason` (the registry ID for snapshot notes). Notes are non-deliverable and
excluded from every wire-replay projection — the real text stays in its actual
carrier; the note is a pointer, never a second copy. The closed `noteKind`
vocabulary and per-segment `injectionId` are declared in
`contracts/conversation_sync_v3.yaml`; `TurnSystemNoteSegment.turn_display`
flags display-only segments for renderers. The frontend renders by
`noteKind`/`injectionId` from a data-driven presentation table with a generic
fallback, so new kinds never break older clients.

Display grouping is purely cosmetic: each note still occupies its own injection
round, but a *contiguous* run of context-injection / session-context notes
(e.g. the session-start context tail) collapses into ONE merged disclosure
with a count pill and per-item bodies. Grouping stays within an explicit
model-round/task/attempt scope, never crosses a non-note or intervention note,
and never folds a reason-specialized intervention (active-checklist re-pin,
modified-files recall). `InjectionSpec.group_context` and source lane generate
the frontend policy, including known legacy-kind repair; there is no separate
reason whitelist. `InjectionSpec.provenance_field` pairs acquisition metadata
with a unique matching witness inside the disclosure, not an external strip.
Failed/skipped/unmatched acquisition is not described as injected context.
The session witness idempotency watermark advances only after the recorder
accepts the note, so a display failure does not suppress future emission. See
docs/RENDER_CONTRACT.md §System interventions → Context-note grouping.

`tests/context/test_context_composer_notes.py` enforces the emission,
reuse-suppression, disabled-block, and fire-once semantics above.

### Attribution: who a turn is *from* vs. what is *injected*

Two orthogonal axes carry provenance, each with a single source of truth —
neither is inferred from ad-hoc flags by the reader.

* **Who initiated the turn** — the canonical ``_initiator`` vocabulary in
  ``lib/turn_initiation.py`` (`human`, `autopilot`, `proactive`, `timer`,
  `brain`, `peer`, `operator`, `swarm`). Writers stamp it once via
  ``stamp_initiator`` at the turn-creation seam (a fresh timer/proactive/goal/
  peer input projection, or the autopilot simulated user); ``message_queue``
  tags queued rows for operator-vs-peer before dispatch. It rides the
  projection into ``TurnOrigin.initiator`` and the renderer shows a distinct
  labelled banner for every non-human source
  (``classic-conversation-renderers.ts:renderOriginBlock``) — so an autonomous
  turn never masquerades as the human. Do not add a parallel actor enum or a
  flag the reader must pattern-match on; extend ``VALID_INITIATORS`` instead.
* **What was injected mid-turn** — the registry ``injectionId`` /
  ``_contextInjectionId`` on the carrier, projected display-only as a
  `system_note` (the invariant above). This is the "what reached the model
  between rounds" ledger, separate from turn origin.

The two never share a field: a turn has exactly one initiator, and an
attributed turn may still carry zero-or-more injected notes inside it.

## Verification

`tests/context/test_context_injection_registry.py` enforces registry ownership, no-bypass writers (including alias/list-addition negative fixtures), protected optional-budget boundaries, protocol-batch deferral, unique event receipts and bounded diagnostics. `tests/context/test_context_injection_aux.py` covers auxiliary payloads/worker authority and write-only reminders. Neighboring specs: context composer/trace, checklist restoration, token-budget diagnostics, journal acquisition, stream/terminal recovery, inbox delivery, child resume/cancellation, Paper tools and Request Inspector persistence.


## Programmatic dataflow guidance

**Problem:** Exclusive custom-tool tasks could receive skill and credential
directories without `load_skill` or `run_command`. One owned native Request
Inspector receipt showed 516 + 313 tokens advertising unavailable operations.
**Decision:** Acquire automatic directories only when the existing capability
check finds their tool in the wire or task-owned executable catalog. A declared
catalog absence yields `load_skill_not_enabled` or `run_command_not_enabled`
and withdraws a former directory; unknown catalog state yields the corresponding
`_unavailable` reason and retains prior snapshots. Acquisition failure also
retains last-known evidence. A cached skill index cannot bypass the acquisition
gate. Explicitly selected skill bodies remain governed
by their existing activation and owner rules. This adds no cache or worker.
**Alternatives considered:** checking only `has_real_tools` retains misleading
instructions; checking only wire names hides callable deferred tools; removing
explicit selections changes user-authorized workflow content. None is adopted.
The 829-token observation is removable context in that request, not proof of
improved task quality, net programmatic savings, or population-level benefit.

**Problem:** Code action exposure happens after context composition. Checking only
`execute_tools` in the initial catalog omits guidance when the final wire adds
`execute_program`; serial-chain-only advice also misses bulk reduction.
**Decision:** Guidance recognizes both actions in live/deferred schemas and the
server's declared gateway names backed by matching admission-compiled gateway
contracts. Configuration alone supplies no capability. The stable 128-token
block is conditional on the code action actually being exposed; it describes
pagination, returned-data dependencies, private reduction, retained inputs and
semantic handoff. New inputs use invoke; result only reuses completed exact calls.
Required invariant checks belong inside the program on private inputs; ordinary
reads remain appropriate when newer evidence or semantic judgment is needed.
**Problem:** A read-then-return parameter example and advice to inspect unknown
outputs can encourage bulk result transport, followed by a separate derivation
and fresh reads. **Decision:** All model-facing guidance asks for acquisition and
derivation together, with returned fields planned first. Unknown structures use
keys/types or small samples; raw data remains available for semantic inspection.
The stable block remains within 128 tokens, without task/result interpolation or
new state. **Alternatives considered:** silently replacing bulk results with
summaries changes requested business data; fabricating output types invents a
contract; mandatory output suppression prevents legitimate model judgment.
This distinction addresses observed attempts to reuse unread pages and direct
rechecks of every page after a correct private aggregation. It does not certify
business correctness or prevent fresh reads. Guidance never interpolates observed
call chains or tool names.
Explicit off retracts prior guidance even if a transient catalog is absent;
missing capability preserves the existing transient omission behavior.
**Alternatives considered:** Injecting from the toggle alone can promise an
unavailable action. Requiring a pre-composition code schema misses late exposure.
Hiding direct tools forces a choice instead of helping the model select it.
Round-specific instructions change cached context and confuse evidence with
execution authority. This guidance never grants tools or bypasses child approval.

Programmatic guidance reuses action-schema details and stays within its 128-token
contract; avoid duplicating operation descriptions or increasing the budget.

Workspace environment projects the admitted root policy with stable labels, execution paths, canonical paths and access per root. Isolation is repository-specific; an unbound non-Git primary root remains independent of an isolated additional root. Bare relative file paths use primary; command cwd retains its valid sticky directory. History must not relocate paths between roots.
