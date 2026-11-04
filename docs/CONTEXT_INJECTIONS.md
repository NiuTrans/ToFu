# Automatic context injections

Owner: `lib/tasks_pkg/context_composer/`. Domain map: [context engineering](modules/context_engineering.md). The executable inventory is `registry.py`; this contract describes its current semantics, not a point-in-time audit archive.

## Unified interface

Every engine-authored conversational context addition uses the public `lib.tasks_pkg.context_composer` interface. Register its `InjectionSpec` first: stable ID, source, necessity, actual trigger, required/optional status, authority, lifecycle, lane, priority and content-token ceiling. Unknown production IDs fail closed. Providers acquire owner-authorized evidence; they cannot invent placement, priority or budget policy. Registration never grants tool/filesystem permissions.

| Lane | Interface and invariant |
|---|---|
| Snapshot | `context_block(id, content, ...)` then `append_context_blocks`, or ordinary `compose_task_context`. Content-addressed reuse preserves bytes. Registered `platform_static`, `ui_nav_directory`, `parallel_execution`, and `delegate_mode` occupy dedicated system-prefix carriers (one per block, authority-ordered); other blocks stay tail user carriers and verified withdrawal appends one tombstone. A system carrier is withdrawn outright on authoritative absence (replace mode, explicit ablation, non-browser surface, or an orchestration-mode transition); transient acquisition failure retains the previous version with an explicit decision receipt. **Tail amalgamation:** blocks are sorted by `(authority, priority, id)`, so same-authority blocks are adjacent; each maximal same-authority run being appended in one pass folds into ONE `<system-reminder>` (`[Context authority: X; N context blocks]`) whose sections keep their own `tofu-context:<id>` markers. Reuse stays per-block granular — `_managed_block_hashes` hashes the wrapper-independent body section (`_canonical_section`), so a changed sibling re-appends alone while unchanged group-mates are reused verbatim; a lone block keeps its standalone envelope byte-identical. |
| Event/inbox | `append_injection(messages, id, text, task=..., round_num=..., reason=...)`. Preserve payload and chronological identity, including identical retries after compaction. A receipt UUID travels with the carrier. No hand-appended guidance dictionaries, historical-message mutation or duplicate `record_injected_note`; the writer owns note projection for registered recovery events. Non-appended diagnostic/error notes remain segment-owned. |
| Bootstrap contract | Only registered `worker_policy` may append a system contract, before any dialogue. Runtime events cannot choose a role or elevate authority. Swarm/goal-mode workers are first-class conversational agents: after the objective they go through the same `compose_task_context` boundary as the root agent with the parent's resolved config (same ambient blocks, feature gates and manifest under the child's Inspector ID). A bare `system` carrier is emitted only when the inherited prompt is non-empty — with no inherited prompt `worker_policy` opens the request, so strict providers never see an empty position-0 system message. Checkpoint recovery refreshes every registered injection ID (worker policies and ambient blocks) from the fresh bootstrap before retained dialogue, preserving read-only/tool scope at its original authority and dialogue byte-for-byte. |
| Telemetry | `record_injection_diagnostic` accepts numeric/boolean metrics only. `context_budget` cannot append a message or intervention card. |

**Per-sibling worker prefix — Problem:** when same-role siblings sent byte-identical tools+system prefixes (per-run identity after the objective, 2026-10-28 07:16), concurrent siblings on the Sankuai Claude route were spread across upstream cache domains and lost their own cached history (54-tool siblings read their previous round's entry on 49.7% of rounds, against 98.6–99.0% with per-sibling prefixes), while no first call read the shared entry. **Decision:** the run binding's team guidance (assignment ID, swarm_run_id, messaging rules) ends the role part of the `worker_policy` system contract; workers without a run binding keep the plain role contract. Evidence and the live A/B are in [Sibling cache affinity](LLM_COST_OPTIMIZATION.md#sibling-cache-affinity). **Alternatives rejected:** a sibling-shared system entry (the measured loss above); a unique system block after the shared ones (no better in the A/B and it reorders composer-owned carriers); staggering launches or capping same-prefix concurrency (delays user work). Spec: `tests/context/test_context_injection_aux.py`.

Chat Goal workers are continuing conversation agents, not delegated bootstrap
workers: they retain the actual model working set, use the ordinary composer,
and append only Goal-role guidance and synthetic review/directive events. The
separate virtual-user reviewer still uses isolated bootstrap inputs. See
[TASK_EXECUTION_MODES.md](TASK_EXECUTION_MODES.md).

`_policy_prefix.py` owns the static exception: insert after explicit system/developer policy and before dialogue, reuse unchanged bytes, and replace only that engine policy on a genuine profile change. Replace mode with explicit operator policy removes it. Each registered system-placement block owns exactly one dedicated carrier, re-seated in authority order only on a genuine membership/content change; a non-system-role witness never satisfies its reuse. Verified top-level legacy envelopes for `platform_static`, `parallel_execution`, and `delegate_mode` migrate once from mixed user carriers when their producer-recorded hashes match; nested project/memory quotes and all other bytes stay unchanged. Missing/mismatched provenance never promotes content or suppresses fresh native policy; no user/tool XML is interpreted as policy. Old archived requests are never rewritten. This prefix migration/change may invalidate one upstream cache prefix; subsequent unchanged rounds are stable. Static prefix insertion cannot split a tool batch; tail blocks still defer.

Both snapshots and events enforce closed tool-call/result batches. Snapshots defer; inbox producers check `can_append_injection` **before draining**. Human-steer classification and producer-owned delivery ACK remain unchanged. Appended never means consumed or acknowledged.


**Scoped guidance — Problem:** Requiring a delivered raw-file version before
each operation blocked edits and even read-only commands whenever nested rules
were discovered or changed. **Decision:** Before the first
model request, acquire declared-root rules and deliver them as required
per-file `project_scoped_rules_<instance>` blocks through the normal composer
(see "Per-rule guidance delivery" below). Subsequent rounds and turns reuse
each block by actual retained content. Tool access acquires nested
rules, changed raw-file hashes and verified removals for the next closed model
request without refusing edits or commands. Unresolved shell targets do not
create a guidance gate; path permissions and command approval remain separate.
Open tool batches defer injection without blocking execution.
**Alternatives rejected:** A read-only-command exemption still blocks ordinary
edits; disabling acquisition loses applicable instructions. The scoped blocks own rule bodies during task preparation; session
`project_rules` retains topology and journal locators. Rule acquisition remains
bounded and unchanged rounds reuse the blocks without a second rules copy. Tests: `tests/tasks_pkg/test_project_guidance_bootstrap.py`,
`tests/tasks_pkg/test_project_guidance_delta.py` and `tests/project_mod/test_scoped_instructions.py`.
`systemPromptBlocks.disabled` suppresses optional **registry IDs** through the shared interface. Required rules, accepted checklist state, explicit user branch input, human/background/peer delivery and evidence-triggered safety/recovery cannot be suppressed by advisory ablations. Their explicit product modes/permission controls retain their existing owners. Budget diagnostics are not an opt-in model notice.

A task carrying the `THIRD_PARTY_TEXT` execution profile (`lib/tasks_pkg/execution_profile.py`; Feishu third-party senders, [integrations](modules/integrations_api.md#feishu-bridge-authority)) is a profile, not an ablation: `_with_context_policy` acquires it as a request with no project, tools or memory, and the renderer keeps only `platform_static` and `current_date`, withholding required and unregistered blocks too. `append_injection` carriers are outside the renderer and keep their producers. Tests: `tests/feishu/test_feishu_third_party_profile.py`.

**Private-input discovery — Problem:** A model-visible snapshot directory adds changing context, but availability does not establish adoption or a correct, cheaper answer.
**Decision:** `context.programmaticInputs` is a request-local boolean experiment, default false; invalid values fail closed (strict validation raises). Explicit true still respects ownership, live catalog and `systemPromptBlocks.disabled`. The callable `tools.inputs()` and `tools.result({ref})` remain available independently.
**Alternatives considered:** Default-on discovery lacks matched quality/cost evidence; removing the helpers would discard the independently verified exact snapshot boundary. Promoting the directory requires natural reuse and matched answer correctness before cost or latency comparisons.

Context acquisition never reads JOURNAL history, seeds/rotates a journal, writes `.gitignore`, or invents journal-maintenance instructions. Project/user instructions own maintenance; the prompt supplies only a primary-root locator and the historical-evidence boundary.

Protocol output (assistant messages, tool calls/results), explicit user attachments/quotes, compaction summaries and separate workflow requests have their own owners. Interruption recovery appends nothing: it resends the interrupted request ([rewind and resend](contracts/TURN_SETTLEMENT.md#interruption-recovery-rewind-and-resend)). They are not advisory prompt injections. A new exception requires an executable regression and an update to this boundary.

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

**Transition identity — Problem:** `project_path_change` and the `mcpToolsDelta` chip diff against an in-memory per-scope baseline. Compositions with neither a task id nor a conversation (Request Inspector wire snapshots, throwaway tasks) all resolved to the MCP fallback scope `task:anonymous`, so one snapshot's project path surfaced in an unrelated snapshot as a path move, and snapshot mode wrote shared state despite its side-effect-free contract.
**Decision:** Transition tracking requires an owned identity — a task id, or a conversation of a non-transient task. Anonymous compositions neither read nor write baselines and never emit a transition note or chip.
**Alternatives rejected:** A snapshot-only flag leaves every other anonymous caller on one shared baseline; clearing the store between tests hides the product-visible leak; keying anonymous calls by path or config invents an identity that has no previous turn. Test: `tests/context/test_context_composer.py::test_anonymous_compositions_never_share_a_transition_baseline`.

**Fetched evidence — Problem:** `all_search_results_text` is the only corpus `fallback_synthesis` gives the model ("Here are fetched contents … Cite sources.") when a turn ends without prose. Every producer decided entry itself: handlers through their third result element (`is_search`), the streaming prefetch by a tool-name check that omitted `fetch_url`, dedup replays by their cached flag, each recording its raw return before the round aggregate budget settled the model's view. The corpus therefore lacked prefetched pages that the Sources footer cites, held a dedup-replayed page twice, and admitted text that is not web evidence: project-handler failures (repo_map returned `not ok`; the read-before-edit and write-freshness refusals returned `True`), a successful `update_search_settings` (it returned its `ok` flag), the server-staging download receipt, blocked/empty/sparse `browser_read_page` reads and all-failed `fetch_url` batches.
**Decision:** `tool_dispatch._pipeline._record_round_search_evidence` is the only writer. After the round aggregate budget it admits a call's final `toolContent` when the tool is in `SEARCH_EVIDENCE_TOOLS` (`web_search`, `fetch_url`, `browser_read_page`), its producer declared it (the handler's `is_search`, or the prefetch/dedup cache entry that replays it) and the call settled `done`; an identical text moves to the newest position instead of repeating. `is_search` declares web evidence, never success or failure: other handlers return `False`, and `browser_read_page` declares only a read that delivered an accepted page, the URL its row names in the footer. A `web_search` that found nothing still settles `done` with its diagnostic and stays evidence.
**Alternatives rejected:** per-handler flags alone (five handlers used the slot as an ok/failure flag, and one false negative, the prefetch, dropped the footer's pages); deciding by tool name alone (the pipeline would need browser read modes and receipt states); retiring the handler's third element (every handler changes for no behavioral gain); keeping failed family results in the corpus (the fallback would cite as sources pages the footer omits; a single failed `fetch_url` already returned `False`, so only all-failed batches and late results behind a failure verdict change). Tests: `tests/tasks_pkg/test_noprose_fallback_evidence.py`, `tests/tasks_pkg/test_repo_map_handler.py`, `tests/tasks_pkg/test_write_freshness_handler.py`.

## Intervention reason presentation

The human-facing rendering of a note's **reason** is owned by the registry, never hand-duplicated in the frontend. Every spec whose lane can surface a model-visible timeline note (every `note_display_policies()` entry, including the composer's `session_context_committed` witness) declares `InjectionSpec.presentation = NotePresentation(label_key, tone, fallback, glyph)`: `label_key` is a `systemNote.reason*` i18n contract key, `tone` is one of the closed `NOTE_TONES` (`ok`/`warn`/`err`/`info`) that selects an intervention chip's colour, `fallback` is the port-less English string, and `glyph` names the shared frontend icon (`frontend/src/icons.ts`) a context row shows beside its label. Declarations are validated at import (tone membership, key shape, glyph shape); the display policy also carries each block's `authority`, which tints the row's glyph.

Both conversation render surfaces **derive** this vocabulary from the registry rather than maintaining their own maps:

- `scripts/gen_system_note_presentation.mjs` reads `registry.note_presentations()` and emits the typed contract `frontend/src/conversation/ui/system-note-presentation.generated.ts`; the typed renderer imports it.
- The retained JS runtime (`tool_rounds.js`) cannot import the TS contract, so it embeds the same map between `BEGIN/END GENERATED` markers; `scripts/check_note_presentation_parity.mjs` fails if the embed drifts.
- `tests/context/test_context_injection_presentation.py` pins every registry `label_key` to both locales, every displayable reason to a presentation, and every `tone` to a `.sw-note-reason-{tone}` stylesheet rule; `context-note-presentation.test.ts` resolves every `glyph` in the shared icon set.

`npm run generate:note-presentation` regenerates both surfaces; `build:frontend` does the same before composing the retained runtime. `check:frontend` includes `check:note-presentation` (contract staleness + parity), and the published graph's authoring digest includes the backend registry and both generators. Reasons absent from the registry (a newer backend, or a snapshot block with no timeline note) render the neutral `info` chip with the raw reason id, preserving the closed-world fallback. Adding a new intervention reason therefore requires exactly one registry edit plus `generate` — no frontend map edits.

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
| `project_rules` | Task project topology/JOURNAL locator; standalone/remote context retains root rules in full | Required; no advisory clipping |
| `project_scoped_rules` | Accessed-path ancestor guidance from declared roots, one `project_scoped_rules_<instance>` block per rule file; each text is delivered once while it stays in model-visible history (rounds and turns), identical bytes at another path are a one-line scope note, changes supersede and deletions retract at closed model boundaries without an execution gate; after compaction a nested rule returns only for retained work, otherwise at its next access | Required; no advisory clipping; a task's own acquisition rejects over 128 files or 1 MiB rather than omitting rules (inherited, already-visible blocks do not count) |
| `user_context` | Optional enabled, nonempty preferences; not task evidence | 1,000 tokens |
| `memory_guidance` | Short evidence/retention boundary when memory and tools are enabled; no duplicate CRUD tutorial | 180 tokens |
| `skills_index` | Discovery index only with admitted `load_skill` in the frozen wire or executable catalog; task-frozen; bodies on demand; capability removal suppresses even a cached index | 1,400 tokens |
| `ui_nav_directory` | Browser-frontend conversation turns only (Conversation Sync v3 turn authority; `non_browser_surface` otherwise); static contract-generated directory of valid `setting:`/`page:` jump keys; never hand-maintained; degraded silently when the contract is absent; dedicated system carrier, withdrawn off-surface | 1,400 tokens |
| `selected_skills` | Exact `$skill-id` references from current user text; owner-resolved instruction pages and content-hash receipts | 8,000 tokens; at most three initial pages, 6,500-token acquisition target |
| `credential_vault` | Nonempty name-to-variable index only with admitted `run_command` in the frozen wire or executable catalog; never values or permission to use every entry | 1,000 tokens |
| `parallel_execution` | Available spawn lane; current schema owns role/tool scope. Waiting guidance uses explicit returned handles with `poll(wait_s=...)`, never retired discovery names or no-ID dashboard polling. Dedicated system carrier, withdrawn (`superseded_by_delegate_mode`) while `delegate_mode` applies | 128 tokens |
| `programmatic_execution` | Enabled mode and a wire/deferred code action or admission-compiled execution gateway; use the actual exposed action | 128 tokens |
| `programmatic_inputs` | Explicit `context.programmaticInputs=true` experiment (default false), owner-bound captures and admitted program action; versioned references, no arguments or source bodies; failed acquisition or opt-out withdraws stale snapshots | 256 content tokens, 4 entries, 4,096 bytes; omit whole block under global budget pressure |
| `project_context` | Only the exact available owner/project-bound coordinator receives unseen Project Brain narrative and overview; ordinary workers use scoped inbox/overlap notices, never the global feed. Absent pages, disabling and acquisition failures stay diagnostic-only. Only a complete admitted/reused page may await provider-success cursor ACK | Optional; 1,800 tokens; omit whole page under block/global budget pressure |
| `project_presence` | Live same-project worker status for the bound Coordinator only; reads `chat_task_runtime.snapshot_owned()` in-memory state, filtered to the same `projectPath`. Terminal tasks age out after ~5 minutes. Content-addressed reuse suppresses re-append when worker status is unchanged. No DB writes, no new storage. | Optional; 1,500 tokens; omit whole block under block/global budget pressure |
| `turn_requirements` | Current owner objective, criterion generations and declared output scope for the bound ordinary assistant Turn; refreshed at final admission and after compaction/retry. Failed acquisition refuses dispatch. See [review contract](contracts/ARTIFACT_REVIEW.md#shared-execution-context) | Required; complete snapshot, no local clipping; canonical prompt limit |
| `active_checklist` | Restore only when no complete current accepted snapshot survives: a paired direct/gateway todo result (including load/lookup) or owned recovery carrier proves visibility, without any hash in its prose. Keep at most one current recovery carrier; preserve real dialogue and audit history. Compaction carries state transactionally inside its own projection. See [continuity contract](CONTEXT_COMPACTION.md#authoritative-checklist-restoration) | Required; no advisory clipping |
| `working_facts` | Conversation-scoped version-bound evidence; newest complete facts, explicit unknown freshness on acquisition failure; owner corrections supersede old snapshots; see [contract](contracts/WORKING_FACTS.md) | 4,096 tokens; omitted count; content-addressed reuse; optional initial block, required correction |
| `task_state_v1` | Required projection only with explicit global-context-plan feature | 2,400 tokens |
| `relevant_memories` | Optional high-confidence owner-scoped retrieval from the latest authored request; evidence, not instructions. No match adds no prompt/timeline notice and does not invalidate historical evidence. Explicit disabling withdraws the block; provider failure retains last-known evidence with a diagnostic receipt, never claiming a fresh empty selection | 1,500 tokens; zero appended tokens for no match |
| `plan_mode` | Contract only while explicitly enabled | Required; 900 tokens |
| `delegate_mode` | Orchestration contract only when Delegate Mode actually narrowed the root at assembly (a catalog without `spawn_agents` keeps the ordinary surface and gets no contract); dedicated system carrier, withdrawn with `delegate_mode_off`. See [Delegate Mode](TASK_EXECUTION_MODES.md#delegate-mode) | Required; 900 tokens |
| `environment` | Current routing facts and authenticated owner browser-extension connection evidence; multi-root/remote warnings only when applicable; explicit environment-off mode respected | Required when enabled; 400 tokens |
| `project_path_change` | One warning for an observed root move; never first sight/steady state | Required; 160 tokens |
| `mcp_tools_delta` | Reachable names absent from frozen wire, with discovery/execution available; no partial schemas. Under `mcpToolExposure='delegate'` it renders the WHOLE connected catalog (wire is empty by construction, no freeze state written) and adds spawn_agents delegation guidance | 700 tokens; at most 8 names + 80-character hints |
| `disclosed_tools` | Deferred name/signature handles from accepted disclosures; retract removal/gateway loss | 700 tokens; at most 16 names |
| `current_date` | Reuse the latest complete owned clock section or top-level system/developer date fact still in the working input. Reintroduce after loss/compaction or a day change; user/tool quotes, XML excerpts and fenced examples are not witnesses. No independent watermark/cache | 80 tokens; zero appended tokens when present |
| `modified_files` | Only files changed OUTSIDE this conversation (other-conversation/unattributed journal records stamped at/after task start); own-conv writes excluded; fires the round after detection, never repeats an unchanged file set (task-held signature watermark); journal poll throttled to once per few seconds of task time | 1,600 tokens; at most 5 paths |
| `tool_progress` | Corrections for observed no-ops/redundant reads/unresolved evidence, not productive serial work | Required; breaker-owned caps |
| `execution_allowance` | Finite invocation allowance after pre-notice full-prompt measurement and before final admission: input/output headroom after tree/local consumption and live reservations, effective tool slots and earliest deadline. Reports an input-only comparable-request upper bound excluding output/notice/growth/provider adaptation; 6/3/1 settled-headroom request milestones complement 50/20/5-percent milestones. Required archive/review handoffs remain original checks; never waives original checks or role permissions | Required; final prompt is remeasured after an appended notice; 350 tokens per notice, compact complete facts when needed; if still over the ceiling, explicitly defer in Inspector without advancing the watermark; at most four finite milestones plus one unknown-usage observation per invocation. Emitted as a round-lifecycle snapshot block so the renderer's content-addressed reuse supersedes the prior carrier (and compaction retires superseded copies) instead of stacking a permanent bare `_isMeta` message per milestone |
| `tool_stagnation` | Repeated identical outcomes, not elapsed time or call count | Required; breaker-owned caps |
| `tool_repeat_reminder` | Early exact-repeat notice one rung below the corrective nudge; never vetoes, audits on its own ledger, cleared by a real human steer | Required; once per repeat digest |
| `partial_stream_retry` | Historical receipts only; a partial transport cut now rewinds and resends the unchanged request | Required; recovery-owned caps |
| `terminal_recovery` | Empty terminal output or unexecuted packet | Required; at most two retries |
| `intermediate_response` | Provider-marked commentary-only output, not every short answer | Required; bounded retries |
| `output_contract` | Delegated worker's `stop` answer fails its spawn `output_schema` (parse or JSON-pointer validation error); asks for only the corrected object. No timeline note; the vetoed answer is kept as continuation prose | Required; once per worker, then settles `completed` with `contract_error` |
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
| `worker_team` | Retired: per-run identity now ends `worker_policy`; registered only for carriers in checkpoints written while the identity followed the objective | Never appended; checkpoint rebuild drops such carriers and refreshes `worker_policy` from the current binding |
| `worker_artifacts` | Optional shared-artifact discovery; names only, not bodies | At most 20 short keys; 1,600-character key hint |
| `worker_dependencies` | Completed explicit prerequisite evidence, separate from delegated user context | Required; scheduler-owned evidence bound |
| `worker_wrapup` | One tool-less honest partial report after halt without final answer | Required; one wrap-up attempt |
| `goal_worker_directive` | Pending engine control for the continuing Goal worker; not replayed handoff text | Required; finite graph budget |
| `goal_worker_review` | Full latest completed VU review appended once as synthetic evidence, never human authorization; no reviewer tool trace | Required; ordinary working-set compaction |
| `virtual_user_directive` | Pinned objective and simulated-user role for explicit autopilot decision turn | Required; existing VU version/classification preserved |
| `context_budget` | **Remove from model context**: engine-owned capacity offers no useful model action | Telemetry only; zero prompt tokens/cards |

## Conversation visibility invariant

Every runtime context addition to a task with a Turn projection has a declared
display destination: a display-only `system_note` by default, or an explicitly
registered dedicated sidecar owner. Bootstrap-only policy belongs to Request
Inspector; protocol-owned assistant/tool messages and compaction retain their
existing owners rather than being copied into notes. Emission is
**note-after-commit**: the composer append boundaries
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

### Runtime input visibility ownership

**Problem:** sharing the `inbox_delivery` carrier did not imply sharing its
presentation. Completion reports wrote `_inboxInjects`, but team/project messages
only entered model messages; a carrier-wide no-note assumption hid them. A
publication before sidecar mutation also froze an incomplete live projection.

**Decision:** `append_injection` defaults every event/inbox writer with a task to
`system_note`, including a generic `context-injection` fallback when the registry
has no specialized kind. `inbox_delivery` presents as an ungrouped incoming
message, not as a user request or a consumed-message acknowledgement. Dedicated
inbox/peer drain owners opt out explicitly with a validated `display_owner`;
`record_note=False` alone is rejected. The content-free append manifest records
`displaySurface`, `displayOwner`, `displayReason`, and the generic note's `blockId`.
These are bounded diagnostic receipts, not a second mailbox or durable ACK store.
A sidecar declaration is the producer's contract, not proof of provider consumption.

Carrier, generic note and append manifest form one synchronous transaction: any
recorder failure restores all three, including evicted manifest history/counters.
A retry therefore cannot leave an invisible carrier or duplicate the failed input.
Task-local path-overlap advice has no dedicated lane: it uses a separate generic
note carrier, even alongside completion receipts. Its queue stays ephemeral and
it creates no durable inbox/feed entry; the note records only the actual append.
A failed advice recorder retains that advice for retry without discarding other
lanes' already-appended payloads, sidecars, or deferred confirmation handles.
At event capture, `INJECTED_NOTES_FIELD` is frozen along with the other projection
inputs. The Turn producer passes it into the existing segment normalizer, which
uses the same note assembly owner as checkpoint/settlement. Stable block identity
makes repeated captures idempotent and grouped-note updates replace the same
block. Private injection buffers are not exposed in the public projection and
snapshot reads never synthesize changes to a live revision. Dedicated drains
populate **all** affected sidecars before publishing their first structural event;
a failed observer cannot erase already-recorded display evidence. This adds no
transport, queue, timer, or per-user authority shortcut.

**Alternatives rejected:** another swarm-only card leaves the next producer
silent; treating every carrier as already visualized conflates distinct sources;
waiting for checkpoint makes live truth lag; copying backend Messages directly to
the browser leaks protocol/private material and creates a second history owner.
Not every internal field is user-visible: omission must belong to an explicit
policy, not an absent renderer. Existing note text limits still apply; a timeline
receipt is not a claim of lossless full-context export or model consumption.

Regression boundaries: registry fallback/explicit ownership, actual team/project
writer → captured Turn projection, first completion event and observer failure,
repeated projection/retry identity, mixed completion + independent message,
unknown-kind fallback, and real application HTTP/browser reload. New injection
producers join the shared writer's bypass guard and these behavior tests; adding
only a source-specific stylesheet is insufficient.

### Attribution: who a turn is *from* vs. what is *injected*

Two orthogonal axes carry provenance, each with a single source of truth —
neither is inferred from ad-hoc flags by the reader.

* **Who initiated the turn** — the canonical ``_initiator`` vocabulary in
  ``lib/turn_initiation.py`` (`human`, `autopilot`, `proactive`, `timer`,
  `brain`, `peer`, `operator`, `swarm`). Writers stamp it once via
  ``stamp_initiator`` at the turn-creation seam (a fresh timer/proactive/goal/
  peer input projection, or the autopilot simulated user); ``message_queue``
  tags queued rows for operator-vs-peer before dispatch. It rides the
  projection into ``TurnOrigin.initiator``; the Turn header's localized role
  label names every non-human source, and
  ``renderers/context-blocks.ts:renderOriginBlock`` adds a block only when the
  origin carries more than that label (peer/operator source, Brain dispatch
  details) — so an autonomous turn never masquerades as the human and the
  label is never shown twice. Do not add a parallel actor enum or a
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

Workspace environment projects the admitted root policy with stable labels, execution paths, canonical paths and access per root. Isolation is repository-specific; an unbound non-Git primary root remains independent of an isolated additional root. Bare relative file paths use primary; command cwd retains the last explicit `working_dir` (a `cd` inside a command never persists). History must not relocate paths between roots.

## Accessed-path project guidance

**Problem:** root-only rules miss nested `AGENTS.md`, and command/background
CWD changes can execute against rules for another directory. **Decision:**
`lib/project_mod/instructions.py` acquires only declared-root/target ancestors;
`lib/tasks_pkg/project_guidance.py` holds the task-owned acquired rules and
delivers each file as its own content-addressed block. Nested sources carry their
own subtree label.
Read acquisition is descriptor-relative/no-follow on POSIX and stops at remaining
aggregate file/byte budgets, without clipping or publishing a partial snapshot.
Verified removals are pruned before validating replacement bounds.
One tool admission memoizes rule reads by their exact arguments (path, root and
remaining byte/file budget), so root rules shared by several targets are read
once per admission; nothing is cached across admissions.

Project command dispatch pins explicit/sticky CWD once before guidance and
foreground/background handoff. The shared command parser's concrete targets
survive an incomplete aggregate parse. Unresolved CD/group structure and dynamic
write targets refuse without execution and request explicit scoped operations.
Opaque interpreter effects remain cooperative and retain existing command/write
admission; this is not an all-program filesystem sandbox. **Alternatives
rejected:** recursive instruction scans mix siblings and tax idle; tool-output
clipping loses mandatory rules; post-gate CWD resolution races detached work.
Executable specifications: `tests/project_mod/test_scoped_instructions.py`,
context refresh-failure tests and the workspace/browser acceptance ladder.

### One initial project-rule snapshot

**Problem.** Task startup included root rule bodies in both session project
context and scoped guidance, inflating required, non-compacting input.
**Decision.** Ordinary task preparation obtains project topology/journal
locators without rule bodies and delivers complete raw-file-witness guidance
through `project_scoped_rules` blocks before admission, including native
preparation. Later path reads reuse the same guidance owner for changes and removals.
Standalone context callers and remote projects retain their existing complete
context. No required file is clipped or silently omitted.
**Alternative rejected.** Inferring delivery from prose cannot preserve raw
file hashes or distinguish changed instructions. Verify
`tests/tasks_pkg/test_project_guidance_bootstrap.py`.

### Per-rule guidance delivery (owner decision 4, 2026-10-28)

**Problem.** Guidance (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `COPILOT.md`,
nested `AGENTS.md`) travelled as ONE `project_scoped_rules` snapshot of every
rule file the task had acquired. The composer reuses a block only when its hash
equals the latest copy in history, so every newly touched directory with a
nested `AGENTS.md`, every new turn (a fresh task starts from the root-only set)
and every linked worktree (`<root>/.tofu/worktrees/<name>`: identical bytes at
new paths) appended the whole acquired set again, and each copy stayed in the
prompt and was re-read on every later round. Logs of 2026-10-27/28 show 79
re-sends across 30 tasks appending 4.2 MB (one task: 6 appends, 716 KB). The
block was also re-rendered and re-tokenized every round (16-158 KB) when nothing
changed. The repository owner decided (2026-10-28) that guidance must never be
re-sent and must cost context only for genuinely new rules.

**Decision.** `lib/tasks_pkg/project_guidance.py` delivers each rule file as its
own required block `project_scoped_rules_<instance>` (`context_block(...,
instance=...)`; the instance is a path digest prefixed by path depth so root
rules precede the nested rules that specialize them). Presence is
content-addressed in the actual messages (`verified_block_hashes`,
`block_content_hash`), so the composer appends only blocks that are missing or
changed:

- an unchanged file is delivered at most once while its block stays in
  model-visible history, across rounds and turns;
- identical bytes at another path render as one line, `Source: <path> — same
  bytes as <delivered path>; that text applies within <scope> too and is not
  repeated here.`; the full text is held by a copy already in history when one
  exists, otherwise by the first acquired path;
- a changed file appends one new version, which the composer prefixes with its
  supersede notice; a deleted file appends one line `Source: <path> — deleted;
  its earlier text no longer applies within <scope>.`; an emptied file a similar
  line;
- when compaction or a history rebuild dropped a block, a root rule is always
  re-delivered and a nested rule only while a tool call still in history
  targets its scope; any other rule is forgotten and the next access acquires
  it again like a first touch (see "Post-compaction rule restoration" below);
  snapshot compaction (`compact_context_snapshots`, now resolving
  instance ids through `block_spec`) retires superseded per-file versions, and
  memory-only head truncation protects the newest version of each required one;
- after accepted L2 restoration, that same snapshot retirement runs over the
  recomposed view too: a freshly acquired replacement removes the verified old
  section immediately, while current siblings and authored text survive.
  This boundary has already accepted a cache rewrite; ordinary rounds still
  append changes without rewriting consumed history;
- a new task re-reads, once, the rule files whose blocks its inherited history
  carries: unchanged ones cost nothing, changed or deleted ones (or files now
  outside the declared roots, or unreadable) get a superseding version or a
  retraction. These inherited paths do not count toward the task's own 128-file
  / 1 MiB acquisition bound, because they add no new bytes; a path the task
  touches again counts again;
- a combined snapshot written by the earlier code (bare `project_scoped_rules`
  id) is retired once with a one-line note, its sources are adopted and
  delivered per file, a one-time migration cost per conversation;
- with nothing changed, round preparation checks only the identity of the
  carrier messages that held the delivered blocks (strong references; a dropped,
  copied or edited carrier forces content-addressed verification) and renders,
  hashes or tokenizes no guidance.

The registry entry stays `required=True`; its stability and lifecycle become
`conversation` because a block now stays valid across turns until its file
changes. `Applies only within: <scope>` stays on every full block and every
one-line note names its own scope.

**Measurement.** Replay of task `9c90d9a7` (Request Inspector tool sequence, its
12 delivered rule files rebuilt in a temporary tree) plus a third turn that
revisits the second turn's paths: 1,133,260 appended characters before, 114,918
after (the distinct rule text is 111,705). Synthetic walk root ->
`lib/tasks_pkg/...` -> `.tofu/worktrees/x/...` -> second and third turn with the
real rule files: 539,828 before, 47,255 after (distinct text 43,258); the later
turns append nothing. An unchanged round's guidance refresh took 9.9-32.8 ms
before and 0.02-0.2 ms after.

**Alternatives rejected.** Keeping one snapshot and appending a diff of it keeps
a growing aggregate identity, so any compaction or turn boundary re-sends it
whole. A content-hash-only block id cannot express a changed file at the same
path as superseding, so stale text would stay current. A durable "already
delivered" ledger outside the messages cannot see compaction or a rebuilt
history and would suppress rules the model no longer has. Re-sending inherited
rules at each new task (the old behaviour) is exactly the cost being removed;
trusting inherited blocks without re-reading their files would keep deleted or
changed rules current. Counting inherited rules toward the acquisition bound
would refuse long conversations that merely accumulated history.

### Post-compaction rule restoration (owner decision, 2026-11-04)

**Problem.** Compaction removes the carriers of every delivered rule, and the
rule above restored all of them, so one compaction re-sent every rule file the
task had ever touched. The count grew with the directories a long task visited,
not with what its remaining work needed. Conversation `mw10w4j3ghfsbk`
(2026-11-04 12:21:08) re-sent 42 rule blocks, 139,706 characters (about 28K
tokens), immediately after a compaction whose summary had replaced the tool
calls that acquired them. That alone was more than the compacted message list.

**Decision.** `_forget_unretained_rules` in `lib/tasks_pkg/project_guidance.py`
runs before each delivery. A rule whose delivered block is no longer visible
is restored when it is a root rule (its scope is a declared root) or when a
tool call still in history targets its scope. Targets come from the same
argument resolution live acquisition uses (`_tool_targets`). Any other lost
rule is dropped from the task's acquired set, and the next tool access to its
subtree acquires and delivers it exactly like a first touch. Presence remains
content-addressed in the messages. The task-local `delivered` record only
separates a lost block from a never-delivered one.

L2's retained projection also calls the pure `compact_project_guidance` owner:
verified delivered nested sections unrelated to the retained tool targets leave
the candidate even when they occupied the current Turn's managed shell. Root
rules, active scopes and full-text holders referenced by active scope notes stay
verbatim. Invalid metadata, unknown acquired state, human steering and media
prevent retirement. Mixed carriers keep their other sections. Final capacity and
economic restoration use the same projection; failed candidates change neither
history nor the acquired/delivered state. Ordinary refresh forgets the retired
rules only after successful adoption. This closes the gap where never dropping
a managed shell prevented the restoration policy above from taking effect.

**Measurement.** Replaying that archive's saved compactor result (25 messages,
9 retained tool calls, all at the repository root or under `.tofu/memories`)
through the new rule restores 1 block (root `AGENTS.md`, 16,592 characters)
instead of 42.

**Trade-off accepted by the owner.** As on a first touch, the first tool call
into a forgotten subtree runs before its rule is visible again; the rule
arrives with the next model request. Acquisition never gated execution.

**Alternatives rejected.** Restoring every lost rule is the cost being
removed. Restoring rules for paths that only the summary or user prose
mentions would guess at intent from text, while tool-call targets are the same
evidence first-touch acquisition uses. Keeping rule carriers out of compaction
would pin every acquired rule for the rest of the task.
Executable specifications: `tests/tasks_pkg/test_project_guidance_delta.py`
(one per requirement plus the end-to-end replay).

### Shared agent startup context

**Problem.** Treating a delegated execution catalog as an all-eager wire catalog
made local prompt admission count every schema before the first model request.
**Decision.** Chat, Coordinator and children share tool preparation. Children inherit
discovery policy restricted to their authorized catalog. The startup admission
estimate uses shared local search/execute projection for auto/local; the unprojected
visible candidates remain available for final provider-native discovery and exact
routed admission. Snapshots inherit those candidates, not the compact estimate.
Required/selected tools remain visible; catalog contracts retain execution authority.
**Alternatives rejected.** Child-only projection forks policy; replacing candidates
with the estimate loses native discovery. Raising limits or summarizing contracts
hides the assembly defect or weakens validation. Verify `tests/tasks_pkg/test_agent_tool_policy.py`
and `tests/swarm/test_swarm_startup_context.py`.

**Exact spawn allowlists and deferred tools — Problem:** a parent could discover an MCP tool through Tool Search and then name it
in a swarm child's exact `tools` allowlist, but the narrowed child held no discovery surface and saw neither the schema nor a
callable wire tool. **Decision:** `SubAgent` passes the exact selected names into the shared child-discovery policy; those names
join the child's required-direct set after intersection with its authorized catalog. Unknown names remain excluded and no search
or execution authority is added. The child can call the delegated deferred tool directly; inherited discovery metadata stays
scoped as before. **Alternatives rejected:** granting discovery tools not requested by the parent widens authority; leaving the
tool deferred makes an exact delegation unusable; routing through the parent's `execute_program` bypasses child admission.
Specs: `tests/swarm/test_swarm_tool_scoping.py` and `tests/tasks_pkg/test_agent_tool_policy.py`.
