# Tool execution and settlement policy

This guide owns side-effect and result-settlement policy referenced by the
[tools domain map](../modules/tools_execution.md).

Verified skill installation accepts the task's existing Auto write grant,
including unattended execution. Manual mode requires an attended decision.
`ToolSpec.auto_approval_tools` is the declaration of this exception; other
receipt-requiring tools retain human confirmation by default. Both approval
paths mint an exact, expiring, single-use gateway receipt before the handler
can install. Scope, origin verification, bounded attempts, and immediate
instruction disclosure are owned by [the skills contract](../modules/skills.md).

## Approval delegation (`set_approval_delegate`)

The orchestrator may register a task-scoped, natural-language approval policy
through `set_approval_delegate`; a call that would reach the human
write-approval gate is then judged first by a bounded, tool-less adjudicator
child invocation against that policy (`lib/tasks_pkg/approval_delegate.py`).
The adjudicator is a third receipt authority (`agent_adjudicator`, beside
`human` and `auto_write_mode`): an approve binds a single-use receipt to the
exact call, a deny settles the call rejected with the reason returned to the
orchestrator, and any undecided, malformed, or timed-out verdict falls
through to the ordinary human gate. The delegate never widens the gate:
`auto_approval_tools` remains the only Auto exception, destructive-command
analysis still runs first, child invocations are never adjudicated, the
policy dies with its task and is not inherited, wildcard scopes are rejected,
and `set_approval_delegate` is stripped from sub-agents
(`SUB_AGENT_DENYLIST`) and read-only waves (`READ_ONLY_AGENT_EXTRA_BAN`).

## Exact settlement and identity

Explicit task `toolSpecAllowlist` and `toolSpecDenylist` select registered
`ToolSpec.key` groups, not individual function names (`project` owns
`run_command`; `evolution` owns `evolution_explore`). Unknown keys or malformed
lists fail before registry builders and provider dispatch; the turn clears its
tool authority and reports the configuration error instead of continuing
text-only. An empty allowlist still selects no groups; absent/null allowlist
leaves ordinary capability gates in charge. Known keys never override owner,
plugin, role or capability gates. Explicit caller-provided tool schemas and
already-authorized child invocation tools retain their separate assembly path.

**Problem:** silent filtering can remove tools required by a long experiment,
or ignore a misspelled denial while the model continues spending. **Decision:**
validate both selections at the registry owner and propagate their typed error
through the task assembly boundary. **Alternatives:** automatic function-name
translation could grant an entire write-capable group unexpectedly; text-only
fallback changes the requested task. Neither is used for explicit policy errors.

A tool call is settled exactly once, including cancellation and failure. The
runtime distinguishes transport failure, invalid arguments, unavailable tool,
permission denial, handler failure, and user abort. Repair is limited to
syntax/shape problems and cannot silently change the requested capability.

Retries of side-effecting tools require an idempotency contract. The generic
LLM retry loop must not replay a completed write, webhook, browser action, or
human-guidance resolution.

`execute_tools` receipts key call ID with canonical arguments, model round, and
world version because a provider may recycle a positional call ID. A proven
redispatch of the same gateway occurrence may reuse its receipt; changed calls
execute, and a cached failure retains its failure verdict. The task-local table
evicts oldest entries beyond 256.

Call IDs are correlation tokens, never conversation- or process-global
execution identities. Fresh assistant batches repair blank or recycled IDs
before history is committed. Each dispatch indexes earlier/enclosing IDs once,
excluding its current assistant carrier and current round. Settled content
belongs to one pipeline invocation and leaves scope after its aggregate-budget
barrier, including across nested gateway dispatch.

Every ordered position inside one provider response executes and settles
independently, including exact twins, after provider-response admission. Duplicate IDs are reminted rather than used to collapse siblings. The narrow routing-repair exception applies when
the same response has an `execute_tools` carrier: post-authority direct calls
donate canonical name/argument identities, and matching `calls[]` children are
consumed FIFO with a `delegated` receipt. Same-channel twins, changed arguments,
different responses, and JavaScript programs remain independent. Malformed
occurrences receive typed rejection and never borrow a sibling. Historical
consumers pair only an adjacent assistant/result run by ID occurrence;
ambiguity fails closed.

**Repeated provider-call admission.** Problem: one response can contain
hundreds of calls with the same name, JSON arguments and caller authority but
fresh IDs. Callback-only suppression neither bounds upstream generation nor
prevents final parsing from announcing the calls again; synthetic duplicate
results also falsely equate distinct response positions. Decision: one shared
`lib/llm/tool_call_flood.py` guard counts canonical identities by response position.
The 33rd identical occurrence rejects the entire response with
`ProviderToolCallFloodError` before ordinary dispatch; operators may override
`TOFU_TOOL_CALL_FLOOD_HARD_CAP` (2..4096, default 32). This is a runaway-output
safety boundary, not proof of a gateway retransmission or a total-tool cap.
Raw provider archives establish whether distinct block indices and IDs already
exist upstream. A repeating suffix after independent calls is a provider-output
failure; equal arguments alone cannot establish transport retransmission or an
MCP retry. MCP execution starts after response admission for ordinary MCP tools.
Different arguments/caller authorities and subsequent responses remain
independent. Accepted twins execute and receive their own real paired results.
SSE ingress enforces admission before optional callbacks, independent of
callback availability; final parsing applies the same guard to callback-free
resends and non-streaming responses before creating rows. A rejected response
is never replayed as an adopted assistant/tool batch. The typed failure surfaces
as `tool_loop` with a response-limit explanation, without automatic retry,
model fallback or route-health
penalties; already admitted read-only speculation retains existing cancellation
and pool cleanup. Guard state belongs to one physical provider attempt and
retains only fixed-size digests per position. Streamed rejection carries bounded
native/canonical position evidence and a raw-archive reference when capture is
available (see [logging contract](../LOGGING.md#repeated-tool-call-evidence)).
Fatal exits settle unadopted tool announcements before DONE and persistence,
using the same tool-round terminal sweep as normal loop exits. Alternatives rejected: hiding
cards leaves execution/cost unbounded; silently collapsing equal arguments
changes repeated-action semantics; dropping provider IDs breaks tool/result
pairing; continuing to consume the stream leaves response retention unbounded.

Native program parents receive a task-unique canonical ID while retaining the
provider token as diagnostics; outputs and children bind only to one active
occurrence. Client-mode custom-tool waits key by `(taskId, callId)`, never
replace an existing waiter, and accept only the first resolution across
response, timeout, and cancellation races.

**Delegated receipt replay.** Problem: cold history and Continue promoted
gateway child audit rows to additional assistant actions, even though the
gateway's result already contained the exact same execution receipts. Decision:
the shared causal replay scanner omits an internal `execute_calls` or
`execute_program` child from model projection only when its canonical
`toolResultEvidence.resultId` already occurs in an accepted adapter container's
`toolContent.results` in the same attempt/task/LLM round. Durable child audit,
effect and checkpoint records remain; `delegated_receipt_count` exposes the
projection count. Unknown scopes, compacted/unproven receipts and independent
provider calls retain the prior path. A missing parent result remains a causal
gap. Alternatives rejected: suppressing equal arguments would erase independent
actions; replaying every execution row invents tool calls the model never emitted.
Coverage state lives only for one replay scan and scales with its existing input
rows. A Python 3.12 traced probe of 4,097 rows (4,096 covered children) took
46.35 ms and peaked at 1,664,765 bytes; it adds no resident cache or durable data.
Derived continuation/compaction policy witnesses include the shared tool replay
projection version. Old working contexts rebuild from authoritative Turns after
a projection fix; they cannot bypass the corrected scanner. Durable state is
preserved, and ordinary bounded checkpoint replacement retains its lifecycle.

The gateway remains protocol-only in durable activity. Its terminal payload is
decoded from sparse canonical `toolContent`: validation failures project as
warning-level skipped child rows, while an executed child's lifecycle owns its
typed V2 code, message, and next action. The wrapper is not rendered twice.

## Child process environment

Commands and MCP validate string-only, NUL-free mappings at launch and resolve
executables using the environment actually passed to the child. MCP server
configuration overrides inherited values before readiness checks; an explicit
PATH is its search boundary. Missing tools fail visibly rather than triggering
installation. Built-in commands execute the platform shell from `lib.compat`,
not the shell named in the launcher's ambient SHELL variable. Native pipelines
remain supported; approval, authenticated-download and workspace guards remain.
`lib/tools/process_environment.py` owns mapping validation, installed CLI discovery,
persistent cache defaults and resolution against the final child PATH.
`lib/mcp/stdio_environment.py` assembles the configured MCP mapping before
launcher readiness checks; `lib/project_mod/run_command.py` keeps command
credential admission and workspace Python selection. No caller mutates the
server environment or evaluates login-shell startup files.

The command dispatcher passes the containing authorized root explicitly.
The nearest complete `.venv` from cwd up to that root wins, including after
sticky cwd navigation. On POSIX, an incomplete marked environment keeps shell,
Git and non-Python tools usable; Python/pip PATH launchers fail visibly with
exit 127 instead of selecting the server interpreter. Windows retains the
launch-time refusal. A server activation marker or an ancestor outside the
root cannot select Python. Restricted environment symlinks are checked before
availability handling and remain refused.

Problem: eager dependency preparation turned missing Python into failure of
all commands, preventing diagnosis and repair. Decision: an unavailable source
venv is represented in the private task tree without changing the source;
fail-closed Python launchers live in reconstructible tool cache and disappear
from selection after the task venv is repaired. An existing task environment
wins over a broken source. Bridge creation errors roll back only the newly
created task venv under the workspace lease, so retries do not accept partial
bridges. Alternatives: silently selecting server Python breaks isolation;
refusing every shell command prevents recovery; reinstalling source packages
mutates shared state and hides the original fault.
Recovery verification: `tests/project_mod/test_workspace_python_recovery.py`.
Incomplete-venv environment assembly (100 warm local calls): median 3.73 ms,
p95 4.40 ms; ten cached launchers, zero child processes or per-call files.
Healthy environment selection does not scan PATH or allocate these launchers.
CLI directories include uv's configured bin, `~/.local/bin`, Cargo's bin and
an explicit npm prefix. Installer-created bins work in the same command and
later calls. Commands and MCP reuse the existing `TOFU_MCP_CACHE_DIR` root
(default `data/mcp-cache`); explicit package-manager settings win. Restricted
commands use workspace-local install/cache paths. Installed tool environments
are user-controlled durable artifacts, never cleared as reconnect cleanup.

Use `command -v TOOL` before reinstalling. For Python project dependencies,
create `.venv` once at the root; use `uv tool install PACKAGE` for persistent,
isolated CLI environments. Ordinary pipelines retain native shell semantics.
`export` lasts for one invocation; configuration and persistent installations
are the cross-call mechanism. The read-only diagnostic is
`python3 scripts/check_tool_environment.py --cwd PROJECT --project-root PROJECT
--launcher TOOL`; add `--kind mcp --mcp-config SERVER_JSON` for one configured
MCP server. Reports contain only path selection and executable resolution.

Problem: installation, environment selection and readiness disagreed, causing
false missing-tool reports and repeated installs. Decision: one validated child
mapping with explicit precedence and persistent discovery. Alternatives:
sourcing shell rc files introduces arbitrary startup side effects; reinstalling
on lookup failure hides PATH errors; disabling pip isolation risks the runtime.
Focused verification: `tests/tools/test_process_environment.py` plus command and MCP suites.

Environment assembly measurement (100 warm local calls): median 0.36 ms,
p95 0.40 ms, zero new files or processes without an installer. Persistent
package/tool directories are created by package managers, reused across calls,
and retained with their configured root. No extra worker, timer, automatic
installer or reconnect-time tool deletion is introduced. MCP sets
NODE_USE_ENV_PROXY, retaining explicit options without injecting unsupported
NODE_OPTIONS flags into old Node runtimes.

## Write and project boundaries

Large or binary outputs follow the producing tool's recovery policy; they are
never dumped wholesale into the next model request or logs.

`edit_file` inserts resolve one unique anchor. `replace_all` is ignored for a
unique-anchor insert and never enables multi-site insertion; only `replace`
grants batch semantics.

`replace` applies the supplied replacement text even when it retains the
anchor at either boundary, inside the text, or as a deliberate duplicate.
Redundant argument characters are not a write error: no style-only retry,
automatic conversion to line insertion, or new line breaks are introduced.
Prefer insert operations for new lines; their existing boundary-echo repairs
remain insertion-only and never rewrite replacement content. Root authority,
read/freshness checks, unique matching and atomic writes remain mandatory.
Exact replacement matches preserve existing CRLF and missing-final-newline
bytes; line insertions retain their existing universal-newline behavior.

Python syntax feedback from a unified edit batch compares each file's original
text with its final successfully written text, once per file. Intermediate
in-memory syntax errors that later edits repair do not trigger follow-up work.
Warnings remain advisory and bounded to the existing Python parser budget;
already-invalid files are not newly blamed, and failed writes produce no
success warning. Desktop edits retain their sequential per-edit commits and
use the final committed state for the same batch-level feedback.

Restricted agent/API absolute reads and writes use the same conversation/task
root registry as project-relative dispatch, including `execute_tools` children.
The trusted dispatch identity binds that registry only for the call lifetime;
a missing scoped registry denies access instead of borrowing global roots.
Targets are checked after symlink resolution, so an in-project symlink cannot
grant access outside the registered roots. A tool's requested cwd/path is not
root-registration authority; the runtime must register its project first.

Absolute-path writes outside every registered workspace root never silently
expand the workspace. All model-facing write tools refuse with
`OutsideWorkspaceError` before registry mutation. Only a reissued call carrying
`allow_outside_workspace` after explicit confirmation may register the nearest
existing ancestor and proceed. Temp scratch registers nothing; forbidden system
paths remain refused. Confirmed roots need no repeated confirmation. UI drops
remain stricter and require adding the folder first.

Same-repo worktree exemption (2026-10-04): a write into a git worktree whose
main repo IS a registered root (or whose repo shares its common dir with one)
is a bounded expansion of the current project, not a silent workspace
mutation. It proceeds like a confirmed write — conv-scoped registration,
root-added signal — without requiring `allow_outside_workspace`. A non-git
directory or an unrelated repository still requires explicit confirmation.

Project identity survives directory renames. `GET /api/v1/project/recent`
reports disk liveness. `POST /api/v1/project/recent/relink` validates the new
path and rekeys owner aggregates in one `project.relink` storage command. It
merges recent counts, migrates active and recoverable conversation project
pins, and atomically rekeys the signal-driven Project Brain projection plus
its retained event tail.
`POST /api/v1/project/git-root-hint`
finds the nearest enclosing `.git`
directory or gitfile for subdirectory staging.

## Bounded result envelopes

`tools.resultEnvelope` ships as `v2`. `ToolResultEnvelopeV2` is the internal
budget/evidence record: stable status, summary, at most 64 structured items,
cursor, truncation, byte counts, freshness/evidence ID, and typed recovery. It
is not copied wholesale into provider tool messages.

Settlement splits the record once. Complete text remains text; complete
structured success uses its compact JSON value; partial/error results retain
only actionable non-empty fields. Constant recovery plumbing
(`observation: "new"`, `historicalRecovery: false`, the single-valued
`kind: "source"`) stays server-side and is re-inflated from class defaults
when the sparse projection is read back; so are source-recovery arguments
that would merely restate the still-adjacent tool call — only genuinely
narrowed replay queries (`read_files` slices) project `recovery.arguments`,
and the constant `cursor: "0"` never projects at all. The bounded
`tofu.tool-result-evidence/v1` sidecar carries evidence outside
provider messages. Artifact-policy tools expose at most 8,000 tokens and
store recoverable overflow in the owner-scoped content-addressed repository.
Source-policy tools may use the otherwise-idle 24,000-token round allowance
and never copy overflow into that repository; the aggregate sibling ceiling
stays 24,000 tokens.

`grep_search` source recovery states exactly what was withheld. A capped or
char-limited result ends with a bounded two-line trailer naming the fired
limit and the per-file remainder distribution (counts and line ranges from a
short post-cap drain; index-path skipped chunks get an exact bounded count
pass; hitting the counting ceilings marks the total as a `+` lower bound).
Recovery is `path`/`include` narrowing when the remainder spreads across
files and the `from_line` per-file line floor for paging one file — a
line-number cursor like the `read_files` range, never a match ordinal. A
drain that reaches the backend's natural EOF proves the page complete, so an
exactly cap-sized result is not marked truncated.

`read_files` source recovery preserves requested identities, shares preview
space fairly, retains at most 20 public reads, and narrows replay to a shared
600-line window.
 Reversed line bounds are swapped before narrowing, mirroring
the read handler's repair, so replay stays inside the range that was actually
read.
 `none` recovery produces a bounded preview with no
recoverability claim. The 8,000-token decision uses final model projection and
exact visible bytes. Shortened success is `partial`, never `ok` plus truncation;
producer-side omissions remain authoritative.

Recovery tools stay eagerly callable whenever an artifact reference can be
emitted. Internal envelopes live only through the current aggregate-budget
pass or bounded dedup receipt. Reconstructible overflow expires after 24 hours
by default, with a seven-day hard ceiling and bounded reclamation. Storage
failure returns an honest preview and narrower-rerun hint without an unusable
artifact reference. Paper adapters default to V2; the explicit legacy arm is
fingerprint-isolated from live tasks.

Explicit `read_files` line bounds remain authoritative for project, absolute,
and uploaded text paths; small files are never widened. Bounds are positive and
1-based, and malformed values fail visibly.

## Timeouts and loop guards

Timeout attribution is exact. The pool ceiling (`TOOL_PARALLEL_TIMEOUT`,
default 300 s) bounds a whole parallel batch, and its verdict says so.
`ToolSpec.timeout_ms_by_name` declares a tool's own deadline: on expiry the
call's runtime context is asked to cancel cooperatively, and only that timer's
own firing replaces the late result with the canonical
`Tool execution timed out: <name>` sentinel, an `error` receipt status, and a
`_declaredTimeoutMs` attribution marker. An outer user abort or the pool
ceiling never produces the declared-timeout marker, and ratified human-wait
exemptions (`_SERIAL_BLOCKING_TOOLS`) never arm one. Declarations are
validated at registration (names ⊆ `provides`; integer within
[1 s, 3600 s]) — a bad declaration fails loud at load, not mid-task.

Loop guards correct before they stop. A human steer resets the
exact-repetition chain because it measured an unattended loop the operator
has already redirected (`reset_tool_loop_guard`, called at the steer-drain
boundary). The remind-only rung (`tool_repeat_reminder`) fires once per
repeat digest one step below the corrective nudge: it never vetoes, advances
no escalation counter, and audits on its own `_toolLoopReminders` ledger so
it cannot consume the one-correction-per-turn slot the nudge/stop escalation
and stagnation detectors read.


### Conditional Git guard rejection metadata

The Git guard's pre-execution refusal stamps `rejection.kind = git_guard`,
`condition = competing_workspace_writers_settled_or_acknowledged`, and
`retryPolicy = {mode: after_condition_change, automatic: false}`. The existing
`retryable: true` means the operation can be reconsidered after its condition
changes, not that any automatic continuation may immediately replay it. This
metadata grants no execution authority and changes neither acknowledgement
receipts nor execution leases. Finalization preserves the descriptor in the
failed Turn error; stall recovery and swarm continuation consume this evidence
as specified in `TURN_SETTLEMENT.md` rather than interpreting refusal prose.


Decision: command progress timing uses the task execution clock: the spawn callback
maps the effective timeout budget into that clock's epoch anchor; progress
captures `execTimingAtMs`, `execElapsedMs` and optional `deadlineRemainingMs`
before observation. These are samples, not a continuously updated stopwatch.
The UI formats the latest backend sample and never advances it with its own
clock. Missing samples keep ordinary running status without fabricated time.
This reuses existing progress frames/coalescing and adds no polling request,
thread or queue; existing event byte budgets include the extra numeric fields.
Problem: browser/wall-clock jumps changed command countdown and elapsed time.
Alternative rejected: correcting a client stopwatch still estimates unobserved
execution and creates a second timing authority. Backend sampling keeps the
actual spawn boundary, effective timeout and observer delay distinguishable.
Local measurement (`benchmarks/execution_clock.py`, Python 3.12.13,
100,000 samples × five): sampling/update median 921.4 ns; all three compact
numeric fields add 79 bytes to a compact progress-event JSON envelope. This is producer overhead, not a bound on
model/subprocess time. The command's 1 Hz browser ticker and DOM scan are
removed; watcher scheduling countdowns retain their existing owner.

## Unified result surfaces and format policy

**Problem:** canonical capture, model delivery and frontend metadata existed,
but debugging required separate source searches. Reports were often JSON
strings containing escaped newlines; a field mismatch in async completion
cards could hide the report while the model received it.
**Decision:** `contracts/tool_result_surfaces_v1.json` owns result source
precedence, model/frontend format policy, notification report tags, and bounded
audit limits. `scripts/gen_tool_result_surfaces.py` derives the Python and
TypeScript policy; `npm run check:tool-result-surfaces` gates drift. Unknown
built-in, custom and MCP tools retain their producer output by default.
`lib/tools/result_surfaces.py` exposes `inspect_tool_round`,
`inspect_swarm_notification`, and `inspect_result_document` over existing Turn
projections. No parallel result log, result table, transport, or replay owner is
introduced. Frontend source resolution uses the same generated contract and
honors explicit empty/null results; secondary `results` metadata never becomes
proof of model delivery. The observer reports renderer inputs, not mounted HTML
or proof of provider consumption. Repeated call IDs retain their Turn indices;
unread history, missing evidence, deferred previews and diagnostic truncation
remain explicit. Artifact reads use existing owner-scoped continuation APIs.

`get_agent_result` and only recognized agent-report portions of `poll` use an
opt-in text view before ordinary budgeting. All identities, outer and agent
statuses, errors, partial output and extra fields survive. Unsupported shapes,
opaque command/task output and typed errors remain unchanged. Authorized
formatted reports retain their original canonical JSON in the existing bounded
artifact repository; references remain in the evidence sidecar, without adding
plumbing to a complete model response. A storage-free/ownerless host retains
runtime canonical values without claiming durable retention. Programmatic
`tools.invoke` and result reuse consume original canonical values, not text
views. Frontend report cards render safe Markdown; JSON errors keep the generic
verbatim viewer. XML completion reports share their current/historical tags
with the same authority.

**Alternatives rejected:** returning every internal field wastes context;
flattening every JSON result can destroy cursors, shapes and machine-readable
semantics; persisting another debug copy creates a second lifecycle and source
of truth. Audit findings are advisory: many or empty JSON fields require review,
not automatic deletion. Reports embedded in JSON and leaked internal envelopes
are independently identified. The inspector runs only on demand, scans bounded
JSON and caps records and previews. No model call, timer or background worker is
added. Tests: `tests/tools/test_result_surfaces.py`,
`tests/diagnostics/test_inspect_conversation_api.py`, and
`tests/visual/test_visual_tool_result_surfaces.py`.

### Local MCP file paths

`contracts/mcp_local_paths_v1.schema.json` owns the opt-in `tofu/local-paths` metadata
and maintained exact adapters for xuecheng `prepare_doc_edit.out_path` and `update_doc.from_file`.
Only connected stdio servers share host workspace paths; network servers and desktop files cannot claim this mapping.
Unrelated parameters remain opaque: names such as `path` or `out_path` alone confer no policy.
Declared files use admitted stable roots and canonical-to-execution mappings; all targets are checked before side effects.
Unknown roots, traversal outside roots, escaping symlinks, readonly outputs and canonical/peer writes refuse invocation.
Inputs must exist. Declared output parents are prepared only within authorized roots after cancellable lease admission.
Read/write leases cover the call; transport cancellation is forwarded and ambiguous calls are not automatically replayed.
Receipts retain error, structured and media payloads and report effective absolute paths; transport failures never claim not-started.
Path routing grants no remote publication approval or new root, and does not sandbox the external server process.
External file writes retain their server durability/undo contract; directory preparation is not native file-history publication.
Problem: relative native writes and absolute MCP outputs could select different repositories.
Decision: deterministic primary/rootname paths and declared local-file admission shared by direct/progressive calls.
Alternatives rejected: guessing path fields or following file existence changes tool intent and server semantics.

## Search observation quality

**Search observation decision:** Problem—wrong-topic snippets, circuit-open engines and
missing article tables could look like successful research. Decision—`tofu-search`
0.11 separates fetch admission/ranking from observation quality, enforces explicit
site scopes before any fetch, retains at most three uncertain snippet candidates in
diagnostics, and reports failed/unavailable engines even with nonempty results.
The full bounded uncertain set enters the existing result artifact, preserving
follow-up recovery without flooding the preview or creating another store.
Received DOM tables and omitted copy identifiers supplement article extraction.
`lib/search_extract_contract.py::EXTRACTION_SCHEMA` owns the helper response:
findings require exact inspected-source quotes, deterministic source offsets, and
separate unresolved requests. The bounded single helper pass retains original
source; invalid/ungrounded output returns raw evidence through the existing
fail-soft handler. Full received text and exact quotes prove neither page/website
completeness nor semantic entailment. Alternatives rejected: zero-overlap hard
rejection loses aliases/languages; treating fetch success as relevance invents
verification; another adjudicator model increases cost without recovering lost
source. Package/host tests exercise degraded engines, scope violations, late facts,
missing fields, false citations and raw-source fallback.

Resource measurement: a synthetic 1,000-row two-column table (39,887 HTML
characters) produced 42,677 extracted characters (1.07x), took 0.6664 seconds
with tracemalloc enabled, and peaked at 3,663,523 traced bytes on the verification
host. This measures one received-DOM extraction, not network/LLM latency. No
resident cache/concurrency is added; original fetch byte/deadline, expanded-text,
artifact quota and lifetime bounds remain authoritative.


**Canonical preview retention — Problem:** representation differences caused false
missing-source hints, while small omitted originals lacked a MODEL recovery ref.
**Decision:** host-only `source_omitted` defaults false; genuine preview producers
opt in. Authenticated non-private omissions retain originals and expose concrete
MODEL refs regardless of size. Lossless `projection_normalized` and duplicate MCP
representations retain canonical evidence separately without prompting recovery.
Application fields grant no provenance; failed retention advertises no ref.
Private child, owner, quota and release rules stay intact. **Alternative rejected:**
full-source comparisons confuse formatting with omission. Tests: `tests/tools/test_hidden_source_provenance.py`.
