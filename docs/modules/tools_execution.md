# Tools and execution

This domain defines tool visibility, model schemas, validation/dispatch, and result projection; MCP/plugin setup lives in [`../TOOL_PLUGINS.md`](../TOOL_PLUGINS.md) and [`../TOOL_SEARCH_EXECUTION_GATEWAY.md`](../TOOL_SEARCH_EXECUTION_GATEWAY.md). The always-on `inspect_image` schema stays within 350 tokens while retaining original-source rendering, upload/path routing, single-region cost guidance, grid, mixed-unit, and read-only semantics. Its coordinates use the visible frame after EXIF orientation and explicit clockwise `rotate`; explicit `crop` wins, while centre `zoom` runs only without a crop. `sourceSize` is EXIF-normalized, `encodedSourceSize` is the stored pixel matrix, `cropBox` is post-rotate, and the browser applies no further crop.

## Ownership

| Concern | Owner |
|---|---|
| Built-in tool specifications | `lib/tools/*.py` |
| Compiled tool contract and result envelope | `lib/tools/contracts.py`, `lib/tools/result_envelope.py` |
| Registry construction and collision policy | `lib/tools/registry/` |
| Per-request visibility and environment | `lib/tools/tool_env.py` |
| Unified execution gateway and Moonshot MFJS wire subset | `lib/tools/gateway.py`, `lib/tools/moonshot_schema.py` |
| Task/runtime adapters | `lib/tasks_pkg/handlers/` |
| Project file operations and bounded shared tree index | `lib/project_mod/` |
| MCP lifecycle and transport | `lib/mcp/` |
| Browser bridge | `lib/browser/`, browser handler |
| Lazy web-search runtime and host providers | `lib/search_runtime.py`, `lib/search_bridge.py` |
| Tool event vocabulary | [`../EVENTS.md`](../EVENTS.md) and event registry |
| Generated inventory | [`../TOOL_INVENTORY.md`](../TOOL_INVENTORY.md) |

## Execution flow

1. The task policy resolves an explicit visibility allow-list.
2. The registry builds schemas for only those tools and rejects name collisions.
3. The model returns an ordered list of call occurrences with opaque, provider-batch-local IDs and JSON arguments.
4. The gateway validates the name, visibility, arguments, and capability.
5. A focused handler executes with the authenticated request environment.
6. The gateway normalizes success/failure and timestamps.
7. Settlement emits the canonical tool result and adds exactly one matching model-context result.

Tool schemas describe capability; handlers implement it. Schemas stay free of task runtime/HTTP; search activates on first valid use. Once resident, its explicit-domain enum remains capability-derived, while prompt prose keeps only each domain's 96-character bounded purpose and any partial-availability warning; identifier rules and examples have one static owner, and the fully loaded `web_search` schema stays within 1,000 tokens. Runtime-built `fetch_url` retains remote/local routing, authenticated-browser server staging, final-destination verification, batch behavior, and optional whole-page relevance semantics within 400 tokens when the filter is on and 300 when off. The optional relevance gate skips pages below 6,000 characters, selects at most 6,000 query-focused characters, and emits a 32-token verdict. Its irrelevant sentinel is not a wire stop; one upstream 429 ends fail-open review, and rewrite remains opt-in. The server-wide `update_search_settings` schema stays within 550 tokens while retaining pure-read-first, profile, override, trade-off, clamp, global-write, and result-honesty semantics; approval metadata enumerates both profile/override and legacy knobs so a global mutation cannot masquerade as a read.

`ToolContractV2` is the single definition for execution parameters, validation, model/search metadata, detailed help, permission, idempotency, typed errors, programmatic eligibility, and overflow recovery (`artifact`, `source`, or `none`).
Its projections feed provider schemas, discovery, and execution validation; detailed help and parameters are paid only after discovery.
Contract idempotency is independent from result reuse. `ToolSpec.cacheable_tools` owns same-task execution reuse; legacy plugins may omit it to retain historical idempotent-implies-cacheable behavior. Mutable conversation/Project Brain, memory, scheduler, swarm-artifact, and media-check observers explicitly opt out so every read is fresh; the loop/progress guard resolves `ToolSpec.idempotent_tools` separately and therefore keeps read/retry protection without granting stale reuse.
`ToolSpec.unchanged_receipt_tools` is a separate projection policy: execution still occurs, and a byte-identical result becomes a short receipt only while the exact prior model projection remains active and the receipt is at most half its characters plus strictly fewer counted tokens. Compaction/removal restores a full delivery; a producer can also mark one outcome non-cacheable (for example a transient `fetch_url` failure).
 `read_tool_artifact` and `search_tool_artifact` retain their single-item arguments and also accept at most 16 `reads` / `searches`; batches use at most four workers, isolate failures, return input order, and share one 7,000-token output budget that preserves every item identity and continuation cursor before allocating body text fairly.

Registry assembly produces one v2 document per executable name. Argument ingestion may apply bounded, auditable syntax/shape repairs, then validates against that request-owned document; a present epoch fails closed on a missing/malformed document.
Repairs anchor on the indexed built-in schemas — every static schema-owning module is registered and a walk test pins that coverage (MCP/paper shims exempt) — while name-keyed structural envelope transforms run regardless of index coverage: a todo_write payload nested inside its own `todos[0]` unwraps only when the single wrapper element carries a nested `todos` list and no `content`, so genuine items are never touched; contract rejections append the expected-shape hint to the fed-back error so the recovery round can self-correct instead of guessing.
Assistant-history `tool_calls` arguments are rewritten to `{}` only when the raw text is not already a valid JSON object (the gateway's arguments-JSON gate); parseable-but-contract-rejected payloads survive intact as the evidence the fed-back schema error refers to.
`get_conversation` is resident on project or explicit-reference turns; its schema stays within 350 tokens while retaining explicit-request safety, raw-default versus lossy prose semantics, parseable whole-message head+tail delivery, clamp disclosure, and positive exclusive paging. Its `before` remains provider-declared as an integer with minimum one, but the executor treats numeric/string zero as the unambiguous omitted default-window sentinel. This narrow compatibility repair prevents a paid model correction round; negative, boolean, fractional, and malformed cursors, plus `limit=0`, still fail explicitly, and every positive cursor remains exclusive.
The same final check covers root, nested `execute_tools`/ToolScript, swarm, and read-only SSE pre-execution calls; rejection remains `rejected`, never `done`.
Read-only SSE prefetch reuses the launch-probed `TOOL_MAX_PARALLEL_WORKERS` budget with a four-thread hard ceiling and retains at most eight speculative calls per model round; excess occurrences stay visible and use ordinary dispatch, while success, provider break, abort, and exception all close the per-round pool and cancel queued closures. Prefetch and ordinary reuse share one task-local FIFO: `TOFU_TOOL_RESULT_CACHE_CAPACITY` resolves to 64..256 personal entries (128 on the 8 GiB reference), 64 on probe failure, 512 distributed, and never above 1,024. Pressure discards only the oldest optimization receipt for safe live re-execution; the budgeted form refreshes its age. Fresh-unchanged tracking uses an independent map at the same capacity, retains only digests/call/evidence IDs (never another body), cannot evict expensive cached results, and is released with the cache at terminal settlement. The call-ID diagnostic ledger shares that count bound but retains only signature/name/status; it never owns result content or replay authority.
Local ToolScript accepts bounded JSON/Object/Array/String operations and repairs a missing object-member comma only before an unambiguous `key:` token, at most eight times; results retain every kind/offset and infer no value, tool name, expression, or permission. A child may name any exact member of the task executable catalog without a preceding search, including a tool excluded from hosted PTC; request-owned schema validation and the ordinary authority, approval, serialization, and execution pipeline remain mandatory. Child results reach the interpreter through one private 1 MiB per-program lane, split deterministically across parallel siblings; durable/model receipts still use the ordinary result budget, so the private copy never becomes another transcript or UI row. Two consecutive childless authoring failures disable ToolScript for that task and direct the model to bounded `calls[]` batching.

Stable gateway schemas follow registry/model-config assembly, which also freezes their `ToolContractV2` documents; a request never advertises `search_tools`/`execute_tools` without its execution document.
Names use provider-safe ASCII, preserving `mcp__server-name__tool-name` while rejecting whitespace, path separators, dots, and Unicode lookalikes.
Assembly/compilation failure clears tool authority and continues text-only. Paper report/Q&A epochs stamp `degradedReason`: side effects fail closed without aborting main generation. Every agentic Paper owner enters through one guarded chassis: three identical call+world rounds halt before a fourth duplicate execution; independently, finite token and actual-dispatch envelopes reserve the last admitted call for tool-less synthesis even when provider usage is absent or every call differs, and a provider that returns calls after authority removal halts before execution. Halted work fails and synchronous abort raises into its lifecycle without publishing partial artifacts, while interactive tasks alone may receive an explicit aborted outcome for partial display.

`None` is an explicit read-compatible adapter state for standalone legacy callers outside a production-owned tool epoch. It is not equivalent to an empty map: an empty v2 map rejects every call. Production timer polls and
research-only paper runners validate against the exact post-policy schemas sent
to the model. Full report/QA/deepen runners freeze a two-layer epoch once per
run: bounded wire visibility plus a larger server-owned executable catalog.
Both layers and the gateway documents come from one registry snapshot; a search
hit cannot drift to a different schema or permission epoch. Contract compilation
failure removes the unattended tool surface, and rejection remains `rejected`,
never `done`.
Paper epochs omit `ask_human` and every registry-declared
`confirmation_tools` capability: a headless engine cannot mint the attended,
one-use receipt those handlers require. Ordinary writes retain the explicit
audited unattended auto-apply policy.

`tools.schemaBudgetTokens` defaults to `0` (uncapped) for every model; names
never install hidden budgets. A positive value is a model-neutral local Tool
Search target, fitted once after programmatic calling, MCP, and multi-agent
shaping. The code floor (`read_files`, `grep_search`, `find_files`, `edit_file`,
`run_command`) and exact `tool_choice` retain full schemas even if the target is exceeded; the shared project/standalone shell schema preserves no-op, timeout, fresh-process, dedicated-file-tool, safe-grep, browser-credential, text-tool, and steer-handoff guidance within 450 tokens, while base `read_files`/`grep_search` retain their wide-read, range, format, index, regex, batch, context, and count semantics within 450/475 tokens. Multi-root projection may repeat its path-local absolute/`rootname:subdir`/primary-root rule, but adds at most 200 tokens across the six core project tools. Other schemas fit deterministically; any omission keeps
`search_tools` + `execute_tools` live so hidden capabilities remain discoverable.
Under the default routed native exposure, cross-family eager companions declare `ToolSpec.native_route_groups`; the router computes a bounded fixed point over those declarations instead of carrying tool-key exceptions.
The browser server-download family can therefore ride search/fetch/browser signals and explicit bilingual download intent without moving ownership into those specs.
When local multi-agent routing is active, its control lifecycle is one required
unit: `spawn_agents`, delta-oriented no-ID `await_agents`, and explicitly replayable `get_agent_result` all survive the budget.
A budget may not advertise launch while hiding settlement or retrieval.
The canonical `spawn_agents.agents.maxItems` equals the launch-probed wave allowance, and backend admission repeats that check rather than trusting provider-side JSON-Schema enforcement.
The complete seven-role catalogue, exact role tool scopes, denylist, artifact tools, mis-pick recovery, asynchronous settlement, dependency, and objective-quality rules have one model-visible owner in `spawn_agents`, whose schema remains within 1,050 tokens.

The gateway pair targets 600 tokens; neither target may fail a request. The pair is never compacted at runtime—rewriting its description bytes between rounds breaks the provider prompt-cache prefix—so drift past the target only logs a warning. For a fixed tier, hosted-PTC activation names may remain in the task latch but never narrow local ToolScript child authority, and observations never enter the schema, so programmatic activation cannot change its bytes.
After three trailing model rounds each use one reviewed read while the latest decision proves local `execute_tools` was projected, the post-dispatch guard may append one fixed `_isMeta` adoption hint, at most once per task.
After six successful rounds each issue exactly one approved inspection/command-family tool, the same guard may instead append one fixed general round-trip hint; the two hints share one per-task budget.
Neither forces a tool or expands authority: failed/parallel rounds, writes/state changes, approvals, polling, interactive/MCP tools, `search_tools`/`execute_tools`, and genuine user steering remain boundaries, while a safety correction wins the same round and synthetic control/inbox carriers are transparent.
Terminal metadata retains at most one bounded, content-free `programmaticAdoptionNudges` or `toolRoundTripNudges` witness; projection alone never becomes adoption.
`execute_tools` and local `spawn_agents` keep fixed conditional guidance. Policy remains task-owned. Compaction removes stable hints and annotations while preserving JSON-Schema property names and validation.
Final preflight isolates malformed schemas as bounded `tool_schema_rejected` diagnostics; it removes dangling `tools`/`tool_choice` if none survive. Kimi requests receive a copy-on-write MFJS projection at both budget and final wire boundaries, covering built-ins plus dynamic MCP/plugin/custom tools: `parameters` always has root `type: object`; root `anyOf`/`oneOf`/`allOf` is projected to a property-union relaxation because MFJS cannot represent a strict object union there; nested same-node `type` + `anyOf` is distributed exactly; and supported `oneOf`, `allOf`, type-array, and `const` forms are normalized. The final documented-subset pass keeps only MFJS keywords, converts root `definitions` to `$defs`, rewrites supported local references, declares otherwise-implicit required properties, and safely relaxes unsupported annotations, validation keywords, tuples, pattern properties, external references, and mixed/non-scalar enums. A recursive validator then rejects any incompatible shape that escaped projection. Canonical request-owned ToolContracts remain unchanged and strictly validate before execution, so a relaxed wire schema grants no authority. Unrepairable individual tools are isolated instead of rejecting the model request. Projection precedes schema token count, fingerprint, and `tool_wire_projection`, so diagnostics describe the bytes actually sent while non-Kimi and already-clean catalogs retain identity.
Budget omission is not a broken tool, and execution always rechecks current visibility and authority after discovery.

Tool Search accepts at most 512 query characters plus 128-character namespace and cursor fields; its process-wide tokenization LRU retains only inputs up to 1,024 characters. The cache uses launch-probed `TOFU_TOOL_SEARCH_TERM_CACHE_CAPACITY` (personal 512..4,096 entries, distributed 4,096, hard ceiling 16,384). Longer catalog descriptions and private hints are tokenized in full for identical ranking, but bypass the cache so an item-count bound cannot retain arbitrary source bytes. MCP pre-request search derives a 4/8/32 lean/reference/distributed catalog-index LRU and 1,024/2,048/4,096 sticky-state LRU from that same signal. The process-level term capacity resolves once; a launch-materialized positive override is validated/clamped before any unused default probe. Deterministic inverted postings preserve ranking; each index precomputes one read-first fallback tuple containing only shared existing-name references plus the maximum possible phrase-boost length, so low-signal requests do not re-sort and provably overlong prompts do not rescan the catalog. Phrase boosts with searchable terms visit only posting candidates while they cover less than seven eighths of the catalog; denser hits use contiguous tuple iteration, and termless punctuation keeps the full compatibility scan. Sticky state keeps one 64-byte raw-plus-normalized query digest pair plus 32 used/64 active names for 24 hours, every record path enforces capacity, and memory pressure safely clears both reconstructible sets. An exact raw digest hit reuses the stable schema order before Python term iteration; a miss still computes the normalized digest, preserving punctuation-equivalent ordering and live legacy-hex compatibility. State touches move entries to the LRU tail under a process-monotonic clock, so TTL cleanup consumes only the expired prefix plus the oldest live entry instead of scanning all active conversations. The bridge caches a generation-bound ordered reference tuple, content fingerprint, and immutable private search-text mapping, so stable rounds reuse sorting/projection/hashing/sidecar joins; catalog replacement, real disconnect, or an effective disabled-set change invalidates them, while public snapshots remain isolated copies. Registry authority de-duplication seeds one request-local name set and updates it on append instead of rescanning the growing catalog per dynamic tool. Request-owned contract documents use a memoized JSON clone; extension values fall back to `deepcopy`, so provider/search/execution schemas remain independently mutable without a cross-request contract cache.
Local search excludes both current provider-wire names and unchanged schemas returned earlier in the same running task. Its ledger holds at most 512 exact-name/schema-fingerprint pairs, never raw schemas or authority; a schema revision becomes discoverable again.
When hidden task-scoped tools have a live Tool Search path, both prompt profiles add one bounded outcome-based instruction naming representative built-in families without claiming availability. The task catalog, result schema, and execution gateway remain authoritative; cross-tool vocabulary lives in `lib/tools/discovery_vocabulary.py`, while function-specific hints remain on the owning `ToolSpec`.

Bounded `tool_wire_projection` evidence records ordered final names, tokens, and an
opaque fingerprint without full schemas; names or token counts do not prove byte stability.

`list_dir` is absent from new epochs; its bounded historical reader backs `run_command` fast paths. Filesystem `grep` segments keep pipeline placement. Directory-only relative `ls` scans 10,000 entries, returns 1,000, and formats at most 64 KiB. `find <dir> -type f` plus at most one quoted `-name`/`-iname` uses `fd` then a depth/time/250,000-entry bounded walk and returns at most 500 paths, including hidden/ignored files. Other operands, executables, expansions, recursion, expressions, mutation, and pipelines keep shell semantics.
Fast-path results use `$ cmd … [exit code: N]` (reader failures exit 1), so task, metadata, and frontend classifiers treat them as executed commands; permission-denied targets keep the real shell. Direct real-`find` segments receive a 40-second host-compatible timeout before the ancestor/FUSE verdict; other recursive scanners require a narrow target or explicit timeout. Kill switches are `TOFU_RUN_LS_FASTPATH=0` and `TOFU_RUN_FIND_FASTPATH=0`.
A `grep_search` tree-index hit always remains eligible. When no index can serve a directory and a real rg/GNU scan times out, an owner-scoped `(target, include)` circuit skips equivalent live walks for 300 seconds by default; a narrower target, different include glob, different authenticated owner, or ownerless legacy call remains independent. The skipped result states that no scan ran and never reuses file content or partial results. State is content-free and process-local, retains 256 entries by default with a 1,024 hard ceiling, and expires on cooldown or equivalent success. `TOFU_GREP_TIMEOUT_COOLDOWN_S=0` disables it; the cooldown is capped at 900 seconds and `TOFU_GREP_TIMEOUT_CIRCUIT_ENTRIES` cannot exceed 1,024. The shared tree index keeps its 45-second base refresh and 900-second hard trust boundary. A complete rebuild whose sorted path and size columns exactly match the prior in-memory snapshot schedules the next refresh at 90 seconds, then at most 180 seconds; any candidate difference immediately resets to 45 seconds, and a disk-loaded/process-restarted snapshot also starts at 45. `TOFU_TREE_INDEX_STABLE_REFRESH_MAX_S` may tune the stable ceiling only within 1..900 seconds and never beyond 80% of the hard trust age when that age is coherent. Index-backed grep/find results older than the base interval expose compact `age/scheduled` evidence: existing grep contents are still read live and Tofu write tools synchronize or invalidate candidates, while external path/size changes may not yet be reflected. A background warm first restores an evicted local blob: a base-fresh snapshot suppresses the project walk, while a stale-but-trusted snapshot is served during the unchanged refresh. When a synchronized write has invalidated that blob, LRU eviction atomically checkpoints the current in-memory columns first; revision validation prevents an older asynchronous persist from reinstalling superseded columns. A clean or over-budget entry adds no write, while checkpoint failure keeps the blob absent and preserves the rebuild fallback.
A plain Python script/`-m` on a network workspace may receive an owner/workspace/interpreter-scoped host-local `PYTHONPYCACHEPREFIX`; unsafe/inapplicable environments stay unchanged. The reconstructible cache is capped by `TOFU_RUN_PYTHON_CACHE_MAX_MIB`, 100,000 entries, 64 namespaces, seven-day TTL, and 256 MiB reserve. Default `auto` activates after repetition; `TOFU_RUN_PYTHON_CACHE=1|0` forces/disables it.

Eligible single-destination Git HTTPS/SSH and curl/wget commands race lightweight direct/configured-global-proxy probes before spawn, project only the winner into the child, and execute the original command exactly once. Git HTTPS cold races prefer a real smart-HTTP response over a faster SSO redirect and stop as soon as that stronger evidence arrives. Health keys include protocol, host, and port. Explicit proxy/SSH routing, multiple targets, recursive submodules, and unparseable shell shapes retain original semantics; protocols are never rewritten and side-effecting work is never replayed.
HTTP(S) uses a child-only proxy/no-proxy overlay; SSH requires a bounded unauthenticated CONNECT probe and the internal ProxyCommand bridge. Credentialed pool rows remain vault-scoped; shell routing uses its existing environment/config proxy plus credential-free global rows. Proxy credentials stay out of command text, argv, metadata, and logs. Exhausted routes return not-run, while result markers distinguish route, auth/client, network, and pipeline-masked failures.
`NO_PROXY` is the normal HTTP default; command auto-routing may challenge it with a configured global proxy, except loopback/link-local. `TOFU_RUN_NETWORK_RESPECT_NO_PROXY=1` makes it strict; `TOFU_RUN_NETWORK_ROUTE=auto|inherit|direct|proxy` selects the master mode. Connect/read/race defaults are 2/3/5.5 seconds, with eight lazy workers, four proxy candidates, and 64 cached protocol origins.

Normal tools remain model-synchronous: one result; bounded progress is presentation-only. `ToolExecutionContext` wraps task abort, scoped cleanup, deadlines, progress, and output. HTTP/push Stop share one owner-checked manager path; commands use TERM/grace/KILL/reap. Detached lifecycles stay unchanged. See [`../EVENTS.md`](../EVENTS.md).
High-level `produce_video`, `produce_report`, `produce_slides`, and
`produce_research` are explicit detached producers. A successful handler stamps
one accepted-task receipt; after the whole tool batch settles, root chat uses
the established clean-finish path (`finishReason=stop`) and emits a
deterministic acknowledgement. The background task runtime—not another model
round—owns polling, progress, completion, error, and artifact-quality truth.
## Visibility and authority

Tool availability is per request. Installed does not mean visible. Multi-user
deployments default to an explicit allow-list; a dedicated operator may expose
more tools through declared configuration. Plugins, MCP servers, request-level
custom tools, and built-ins all cross the same name/visibility gate.

Project and filesystem tools receive a resolved workspace/root capability; exact UI reconciliation of primary/root/access state is side-effect-free, while any drift follows the normal prune/register path.
They do not infer authority from the process working directory. Write tools use
shared path validation, freshness, atomic replacement, and write-set attribution.
Read results never grant a subsequent write automatically.

Plan Mode is a stricter request policy layered over this boundary. Initial
wire exposure and the Tool Search catalog remove mutating or unproven schemas;
the final dispatch check independently requires the exact call to be proven
read-only. Unknown and caller-defined tools fail closed, MCP tools require an
explicit `readOnlyHint: true`, and mixed desktop tools expose and accept only
their read branches. This also applies to caller-supplied `tools=[...]`;
unknown schemas are removed and the framework-owned canonical `ask_human`
schema is present so within-turn clarification cannot be disabled accidentally.
The policy owner is `lib/tasks_pkg/plan_mode.py`.
Long-running tools normally keep one-call/one-result model semantics: `ToolExecutionContext` scopes sequenced/coalesced UI progress, bounded reconnect state, task-abort fanout and TERM→grace→KILL→reap; overflow spools outside the project under a shared task budget, persists as an opaque owner-scoped expiring artifact, and returns bounded head/tail (partial on cancel/timeout/interrupt), while startup reclaims abandoned spools. Local `run_command` registers its PID/PGID before any spawn callback, then uses a 350 ms live-start grace: a command that settles inside the window emits only its authoritative terminal result, while a command that outlives it (or produces at least 4 KiB early) asynchronously publishes the spawn clock, opens live output, and checkpoints the running round for reconnect recovery. When an attended conversation receives a durable `user-steer` while a real subprocess is running, the handler closes the outstanding tool-call/result pair with one provisional background receipt and immediately releases the model loop to consume the steer. The worker owned stdout/stderr from process start, so handoff cannot strand a pipe; it stops foreground progress, preserves timeout/whole-task Stop, cwd/index/file-diff finalization, and queues the bounded authoritative result as a non-human durable workflow turn. The completion enqueue opportunistically dispatches only when the conversation is idle, closing the enqueue-after-settlement race without adding model-visible poll/read/wait tools or parameters. Explicit stdin waits remain foreground-owned. SubAgent tool proxies (swarm workers and FlowExecutor leaf workers) bridge the cooperative control fields across their isolation membrane: a live `run_command` subprocess pid/pgid is mirrored proxy→parent so the stuck-task reaper arms the gentle `_cmd_interrupt` watchdog instead of force-failing the whole parent (an autopilot run), and a planted interrupt is transferred parent→proxy only while the stamped pid matches, reaching the command's read loop; the parent copy is retracted once the read loop consumes it, and a consumed flag is never re-transferred.
## Side effects and settlement

Each call settles once. Side-effect retries require idempotency, call IDs remain
correlation-only, workspace expansion requires explicit confirmation, and
result overflow follows one bounded recovery contract. The detailed invariants
live in [`../TOOL_EXECUTION_POLICY.md`](../TOOL_EXECUTION_POLICY.md).

## MCP boundary

MCP lifecycle, discovery, health, and transport live under `lib/mcp/`. The registry normalizes the full search/execution authority while active schemas are a smaller wire projection. Auto exposure is owner+conversation scoped and reuses prior calls on low-signal turns before fallback; search cards are not wire evidence. `MCPBridge.get_enabled_tool_summary()` is the sole compact browser/status projection: under the bridge lock it counts only enabled tools on live or parked handles, sorts server names, copies no description or schema, and never performs discovery; API owners may attach it to an existing response, but summary failure cannot fail the owning config/catalog/mutation.
Native MCP `ImageContent` stays out-of-band on a string-compatible result carrier until the task adapter decodes it under per-image, count, and aggregate byte ceilings. The owner-scoped media authority persists each original; tool metadata and model context retain text only, while the authoritative Turn projects bounded attachment references for the existing browser image renderer.

Display titles come from one composer, `lib/tasks_pkg/tool_display` `compose_mcp_display`: resource + container, plus up to two curated operation chips (`method`/`regex`/`field`/`node_id`/`dry_run`) so same-resource parallel calls (e.g. two `get_log_file` reads with different regexes) never share one row title. The live tool line and the persisted results-row title both use it; chips are whitelisted so tokens/payloads can never leak into a title.
At settle, `handlers/mcp.py::_settle_mcp_round_display` refreshes the round with whatever the result taught it: `lib.mcp.project_names` harvests id→name/title pairs and resource URLs (a `read_doc` row swaps the bare contentId for the article title the moment the read lands), the label is re-composed with the same composer, and `_mcpLinks` is re-keyed to the fresh label so the hyperlink still wraps the exact rendered substring. Link hrefs come from `get_project_url`/`get_doc_url`, which prefer a verbatim harvested URL and otherwise synthesize from a learned-or-default base (`www.overleaf.com`, `km.internal.example.com`). MCP error strings settle as an `error` verdict on the round (preserved by `_finalize_tool_round`'s verdict protection, rendered via the failed lane) — there is deliberately no `<server>` / `<server> (error)` badge chip, since the label already names server/tool and success/failure is the round status's job.

Remote content is untrusted tool output. Resource links, text, images, audio, `structuredContent`/annotations preserve declared types and size bounds; the partition keys off `readOnlyHint` alone.

## Invariants

- One registry for names and schemas; one gateway for execution verdicts.
- Visibility is request-scoped and default-deny.
- Tool call IDs are batch-local correlation only; one history index repairs them to unique IDs independent of bounded diagnostic receipts, and historical pairing is adjacent and occurrence-safe.
- Content equality never proves duplicate execution; only a stable-slot transport retransmission can be suppressed before dispatch. Fresh observers execute again and may compact only their redundant model projection.
- Every call settles once on success, failure, timeout, cancellation, or abort.
- An accepted detached producer ends the root model loop; it never authorizes
  model-driven sleep/curl polling.
- Writes use explicit root authority, freshness checks, and atomic operations.
- Handler exceptions become typed tool failures, not successful text.
- Tool results are bounded before model context and logging.
- `compactionLayer='L0'` means content was actually withheld from the model (truncated V2 envelope, legacy placeholder spill, or hard clamp); lossless envelope re-serialization of a complete result is never stamped, so the COMPACTED pill cannot claim a placeholder for a result the model fully saw.
- Artifact references are owner-scoped, expiring, content-addressed, and never
  reveal a database or filesystem path.
- MCP/plugin/custom-tool adapters cannot bypass visibility or egress policy.
- Generated inventory derives from the live registry.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Built-in schema | focused `lib/tools/<domain>.py` | registry and inventory generation |
| Tool contract/result budget | `contracts.py`, `result_envelope.py`, compaction budget | schema compiler, typed error, 8k/24k, cursor tests |
| Handler behavior | focused `lib/tasks_pkg/handlers/` module | gateway settlement and events |
| Tool visibility | `tool_env.py`, registry plugin owner | owner/request isolation |
| Project write/index | `lib/project_mod/` and write gates | root attribution, freshness, rollback, shared resource ceilings |
| MCP server/transport | `lib/mcp/` | lifecycle, health, normalized result |
| Browser action | browser bridge + handler | user scope, queue TTL, auth |

## Test map

```bash
pytest -q tests/test_tool_registry.py tests/test_tool_registry_builtin_name_protection.py
pytest -q tests/test_unified_tool_gateway.py tests/test_tool_settle_all_lanes.py tests/test_unchanged_tool_result_projection.py
pytest -q tests/test_core_tool_isolation.py tests/test_custom_tool_isolation.py
pytest -q tests/test_write_tools_atomic.py tests/test_write_tools_root_attribution.py
pytest -q tests/test_run_command_network_route.py tests/test_subscription_routes.py tests/test_netpath.py
pytest -q tests/test_mcp_v2_protocol.py tests/test_mcp_liveness_probe.py
pytest -q tests/test_mcp_display_modifiers.py tests/test_mcp_tool_links.py
pytest -q tests/test_long_agent_v2_contracts.py tests/test_programmatic_local_backend.py tests/test_tool_attention_contract.py
```
