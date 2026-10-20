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

## Native application approval composition

`NativeGateway` uses the application's canonical decision gate when no explicit
`decision_policy` is supplied. `authorize_prepared_tool_call` admits one already
parsed occurrence through the same task partitions, Auto/Manual exceptions,
Git guard, delegate and human gate as ordinary dispatch. Automatic exceptions
mint the existing `auto_write_mode` receipt; human, delegate and Git decisions
retain their distinct authorities. It executes no handler. Execution consumes
the exact prepared occurrence and its unused receipt through the normal
pipeline, preserving later hooks and authority checks without another parse or
human prompt. An explicit trusted decision-policy port remains available.

Native round delivery supplies factual round identity. Private prepared carriers
join the existing bounded pending-approval record; arguments and carriers are
charged before the gate and rechecked after canonical argument rebinding. Task
tool rows are borrowed. Worker metadata mutations use a short synchronous lock;
publication, decision waits and handlers run outside it. Cancellation joins the
actual worker and withdraws its row/grant. Terminal projection first withdraws
accepted grants not consumed by execution, refusing a still-pending callback.
Denied or faulted decisions execute nothing and preserve their settled tool row.

Problem: native writes required a separate caller policy and could not enter the
existing application approval lifecycle. Decision: expose decision/withdrawal
ports from the same tool-dispatch authority and reuse its parsed occurrence.
Alternatives: duplicating Auto/Git/human rules diverges; treating all native
writes as preapproved bypasses policy; reparsing creates duplicate tool cards.
This supplies native application composition, not production default selection.
Verify native gateway Auto/Manual/denial, actual approval-owner isolation,
cancel/pressure withdrawal, unused grants, hook refusal and installed native
application tests across disposable storage authorities.

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

Problem: reading compatibility `tools` schema arrays as settings failed before provider entry.
Decision: use the shared flag normalizer, preserving declarations and dotted policy. Rejected alternative: dropping valid caller tools.
Verify `test_tool_gateway_contract_assembly.py`: protocol translation, exact contracts and programmatic on/auto/off.

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

Handler presentation is provisional until execution returns and deadline
decisions settle. Problem: an exception after a handler's `done` update kept a
successful card and audit, while cached failures prevented a fresh observation
on a later call. Decision: the common executor corrects the verdict and shares
its effect-recovery error between presentation and model delivery; canonical
settlement evicts non-success observations and emits the execution outcome
audit once. Proven admission refusals may still audit before dispatch.
The pipeline marks its ownership on each round before dispatch; direct executor
consumers (Paper, Timer) audit on executor exit instead of handler presentation.
Alternatives rejected: rewriting exception text cannot correct cached or
persisted state; retrying automatically could duplicate an uncertain write.
Each subsequent call still passes normal authority and recovery admission.
Verify `tests/tasks_pkg/test_tool_outcome_finality.py` for legacy/V2 delivery,
late exceptions, unknown writes, timeout attribution and successful read retry.

**Settled result publication.** Problem: a worker returning after the parallel
deadline could overwrite result cards and emit new handler frames after the
timeout already reached the model. Decision: common result settlement revokes
that occurrence's handler presentation through its existing runtime context.
The shared publication fence protects bounded result mutations and event
admission; enclosing invocation fences remain in force. A worker entering
after collection closed never enters its handler. Late success or failure
cannot replace the frozen result, verdict, timing or outcome audit.
Resource ownership remains registered until worker exit; closing presentation
does not join an observer, prove that external effects stopped, or grant retry.
Alternatives rejected: preserving only the status still permits contradictory
body text; joining an uncooperative handler removes the finite pool deadline;
discarding its runtime context loses cancellation and cleanup ownership.
Verify handler-entry, display-preparation and late-return races in
`tests/tasks_pkg/test_tool_outcome_finality.py`.
The fence lives on the active tool context, with no new worker or global
registry. The process-local closed marker is stripped by the common storage
projection while effect evidence stays intact. A CPython 3.12.13 allocation
sample of 2,000 active contexts measured about 171 bytes per fence; separate
100/1,000/2,000-cycle create/close/unregister samples each retained 208 traced
bytes and zero active contexts. The durable round projection added zero bytes.

**Result collection recovery.** Problem: an omitted completed Future discarded
an available executor receipt and the final fallback claimed the tool was
unknown. Decision: the pipeline harvests missing terminal Future receipts before
ordered replay, using the same recovery path after a collector deadline. It
neither waits again nor re-enters handlers, and an already collected verdict
always wins. Canonical capture, result budgeting, search accumulation and ordered
model delivery keep their existing owners. If no receipt can be recovered,
`tool_result_missing` identifies a collection failure without asserting tool
absence or changing effect evidence. Retry guidance uses the existing
`ToolContractV2.effectRecovery` policy; missing, contradictory or changed
execution evidence requires outcome inspection. Every new call still passes
normal authorization, and independent work may continue. Alternatives rejected:
repeating the handler may duplicate effects; interpreting a missing result as
tool absence sends the model toward the wrong repair; treating all missing
results as safely retryable invents execution authority. Recovery borrows the
current invocation's Future/result maps and adds no pool, retained registry or
durable metadata. Verify `tests/tasks_pkg/test_tool_result_collection.py` for
original success/failure, sibling ordering, search evidence and recovery policy.


Root MCP authority refreshes before each request through ordinary owner/policy
assembly on detached carriers. Only names in the root's initial MCP candidate
or executable surface can return; catalog and execution contract/namespace
metadata publish together. Provider discovery policies/counts stay frozen so
revocation cannot turn searchable tools into eager wire declarations.
Non-MCP grants and frozen wire remain unchanged.
Child and project-dispatch scopes never rebuild the root inventory. This avoids
latching an empty startup catalog after asynchronous MCP connection, and removes
revoked grants without treating durable declarations as authorization.

Frozen MCP declarations are visibility, not execution grants. If an exact MCP
name in the task's wire schema is absent from its live executable catalog,
ordinary ingestion settles it as `tool_unavailable` with
`reason=declared_without_live_authority`, `declared=true`, and
`executable=false`. It runs no handler and incurs no hallucination streak or
autopilot hallucination penalty. Unknown MCP names still follow ordinary
hallucination rejection; an empty live catalog remains deny-all. Reconnection
or restored permission must rebuild live authority before the call can proceed.
The receipt identifies the membership mismatch, not an inferred outage cause.

**Problem:** cache-stable MCP wire declarations can survive disconnect,
discovery failure, or permission removal; using execution membership alone to
label names fabricated misdiagnoses model calls. **Decision:** classify exact
wire-declared/live-absent names before generic name repair, retaining both
membership facts in the refusal. **Alternatives:** unioning declarations into
the execution allowlist revives revoked grants; admitting arbitrary `mcp__`
names bypasses exact membership; rotating frozen wire breaks prefix stability.
Verify assembly → refusal → recovery in
`tests/mcp_protocol/test_mcp_declaration_lifecycle.py`.
Retries of side-effecting tools require an idempotency contract. The generic
LLM retry loop must not replay a completed write, webhook, browser action, or
human-guidance resolution.

**Error recovery guidance.** Problem: a blanket "stop and report" can end useful work, while "retry" after interrupted execution can repeat effects. Decision: the common typed-error fallback scopes recovery to the failed operation, asks for outcome verification before repetition, and allows independent authorized work. `retryable` alone is neither replay authority nor a task-stop instruction. Owner-specific advice and error codes retain precedence. Command watchdog and restart receipts preserve partial output and describe unconfirmed effects; an absent process and exit receipt do not prove failure before execution. Custom-tool timeouts, transport failures and rejected oversized results likewise require outcome inspection. A result rejection does not undo the tool operation. JavaScript failures after child calls prefix specific authoring advice with effect reconciliation and retained-input reuse, in both program entry points and before the bounded hint can truncate it. Missing retained inputs explain the evidence gap; byte-budget failures recommend smaller retained-input groups or artifact pages, never source re-execution. Alternatives rejected: treating all failures as retryable weakens duplicate-effect protection; blanket aborts discard independent progress. Cancellation, admission and settlement policy remain with their existing owners. Runtime failures suggest environment repair or available direct tools; cancellation advice never asks to resume execution.

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

**Handler receipt admission.** Problem: a handler could return another call's
ID or a malformed result, allowing sibling output to be overwritten or a
returned effect to be acknowledged without a usable receipt. Decision: the
common executor validates the three-field result, exact dispatched ID and
boolean search flag before acknowledging return or exposing content to cache
and settlement. Invalid receipts fail the owned occurrence, withhold their
payload and retain the existing effect-recovery policy. Non-replayable writes
remain unknown and require reconciliation; other invalid receipts report
`tool_result_invalid` with adapter-repair guidance. A valid empty content value
remains success. Alternatives rejected: rewriting a foreign ID invents
provenance; gateway-only checks miss direct executor consumers; retrying the
handler can repeat effects. Ordinary approval and cancellation still apply.

Every ordered position inside one provider response executes and settles
independently, including exact twins, after provider-response admission. Duplicate IDs are reminted rather than used to collapse siblings. The narrow routing-repair exception applies when
the same response has an `execute_tools` carrier: post-authority direct calls
donate canonical name/argument identities, and matching `calls[]` children are
consumed FIFO with a `delegated` receipt. Same-channel twins, changed arguments,
different responses, and JavaScript programs remain independent. Malformed
occurrences receive typed rejection and never borrow a sibling. Historical
consumers pair only an adjacent assistant/result run by ID occurrence;
ambiguity fails closed.

**Repeated provider-call salvage (2026-10-18).** Problem: one response can contain hundreds of calls with the same name, JSON arguments and caller authority but fresh IDs. The previous admission rejected the ENTIRE response at the 33rd identical occurrence — a correct runaway boundary, but it threw away every independent call that preceded the flood and surfaced a `tool_loop` error the model could not act on. Decision: one shared `lib/llm/tool_call_flood.py` guard counts canonical identities by response position, and the FIRST repetition of any exact signature (name + canonical JSON arguments + caller authority) triggers a **salvage**, not a rejection: the transport aborts the upstream stream, `finalize` collapses the accumulated prefix to the first `TOFU_TOOL_CALL_FLOOD_HARD_CAP − 1` occurrences of each flooded signature (default cap 2 ⇒ keep one), and the adopted assistant message carries a bounded `_flood_collapsed` marker (`{tool, count, limit, traceId, rawArchiveRef}`) plus a matching `usage._flood_salvage` stamp. The repetition never enters the transcript, so there is no unanswered `tool_use` and no synthetic duplicate result. A salvaged round is adopted as a normal `tool_calls` completion; `stream_handler._analyse` exempts its `_missing_done` evidence from the truncated-stream retry because the abort is OUR client stop and every retained call already parsed as complete JSON. Callback-free resends and non-streaming responses apply the same collapse in `parse_tool_calls` before rows or effects are created. Different arguments/caller authorities and subsequent responses remain independent; the cross-round exact-repetition loop guard still owns a model that floods again on the next turn. Guard state belongs to one physical provider attempt and retains only fixed-size digests per position. Streamed triggers still dump bounded native/canonical position evidence and a raw-archive reference (see [logging contract](../LOGGING.md#repeated-tool-call-evidence)). Alternatives rejected: rejecting the whole response loses the independent prefix and surfaces an unusable error; synthesizing one result per provider id falsely equates distinct positions and breaks strict-vendor replay; collapsing only at dispatch leaves the upstream stream and its cost running.

Native program parents receive a task-unique canonical ID while retaining the
provider token as diagnostics; outputs and children bind only to one active
occurrence. Client-mode custom-tool waits key by `(taskId, callId)`, never
replace an existing waiter, and accept only the first resolution across
response, timeout, and cancellation races.

**Delegated receipt replay.** Problem: cold history and Continue promoted gateway child audit rows to additional assistant actions, even though the gateway's result already contained the exact same execution receipts. Decision: the shared causal replay scanner omits an internal `execute_calls` or `execute_program` child from model projection only when its canonical `toolResultEvidence.resultId` already occurs in an accepted adapter container's `toolContent.results` in the same attempt/task/LLM round. Durable child audit, effect and checkpoint records remain; `delegated_receipt_count` exposes the projection count. Unknown scopes, compacted/unproven receipts and independent provider calls retain the prior path. A missing parent result remains a causal gap. Alternatives rejected: suppressing equal arguments would erase independent actions; replaying every execution row invents tool calls the model never emitted. Coverage state lives only for one replay scan and scales with its existing input rows. A Python 3.12 traced probe of 4,097 rows (4,096 covered children) took 46.35 ms and peaked at 1,664,765 bytes; it adds no resident cache or durable data. Derived continuation/compaction policy witnesses include the shared tool replay projection version. Old working contexts rebuild from authoritative Turns after a projection fix; they cannot bypass the corrected scanner. Durable state is preserved, and ordinary bounded checkpoint replacement retains its lifecycle.

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

Problem: eager dependency preparation turned missing Python into failure of all commands, preventing diagnosis and repair. Decision: an unavailable source venv is represented in the private task tree without changing the source; fail-closed Python launchers live in reconstructible tool cache and disappear from selection after the task venv is repaired. An existing task environment wins over a broken source. Bridge creation errors roll back only the newly created task venv under the workspace lease, so retries do not accept partial bridges. Alternatives: silently selecting server Python breaks isolation; refusing every shell command prevents recovery; reinstalling source packages mutates shared state and hides the original fault. Recovery verification: `tests/project_mod/test_workspace_python_recovery.py`. Incomplete-venv environment assembly (100 warm local calls): median 3.73 ms, p95 4.40 ms; ten cached launchers, zero child processes or per-call files. Healthy environment selection does not scan PATH or allocate these launchers. CLI directories include uv's configured bin, `~/.local/bin`, Cargo's bin and an explicit npm prefix. Installer-created bins work in the same command and later calls. Commands and MCP reuse the existing `TOFU_MCP_CACHE_DIR` root (default `data/mcp-cache`); explicit package-manager settings win. Restricted commands use workspace-local install/cache paths. Installed tool environments are user-controlled durable artifacts, never cleared as reconnect cleanup.

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

Problem: a normally returned error could lose its execution verdict when
serialized, leaving a successful card/cache/audit despite failed canonical
evidence. Decision: host-generated typed errors carry failure metadata through
both serializers; the shared executor and settlement consume that metadata.
MCP's protocol error flag takes precedence over prose prefixes. Successful
responses containing error examples remain data. A failed batch child or an
incomplete source/view does not fail a successfully completed outer operation.
Alternatives rejected: parsing arbitrary result JSON or matching message text
would misclassify documents; changing only a display adapter would leave direct
consumers and canonical receipts inconsistent.
Redaction preserves a producer envelope only while its complete source remains
that same valid envelope with the same status. Independent raw sources never
inherit a view's envelope authority. Typed error code, retryability and recovery
advice survive budgeting without repeating the error inside generic items.
Verify `tests/tasks_pkg/test_tool_outcome_finality.py` and
`tests/tools/test_canonical_tool_result_pipeline.py`.

Settlement splits the record once. Complete text remains text; complete structured success uses its compact JSON value; partial/error results retain only actionable non-empty fields. Constant recovery plumbing (`observation: "new"`, `historicalRecovery: false`, the single-valued `kind: "source"`) stays server-side and is re-inflated from class defaults when the sparse projection is read back; so are source-recovery arguments that would merely restate the still-adjacent tool call — only genuinely narrowed replay queries (`read_files` slices) project `recovery.arguments`, and the constant `cursor: "0"` never projects at all. The bounded `v1` sidecar carries evidence outside provider messages. Artifact-policy tools expose at most 8,000 tokens and store recoverable overflow in the owner-scoped content-addressed repository. Source-policy tools may use the otherwise-idle 24,000-token round allowance and never copy overflow into that repository; the aggregate sibling ceiling stays 24,000 tokens.

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


Decision: command progress timing uses the task execution clock: the spawn callback maps the effective timeout budget into that clock's epoch anchor; progress captures `execTimingAtMs`, `execElapsedMs` and optional `deadlineRemainingMs` before observation. These are samples, not a continuously updated stopwatch. The UI formats the latest backend sample and never advances it with its own clock. Missing samples keep ordinary running status without fabricated time. This reuses existing progress frames/coalescing and adds no polling request, thread or queue; existing event byte budgets include the extra numeric fields. Problem: browser/wall-clock jumps changed command countdown and elapsed time. Alternative rejected: correcting a client stopwatch still estimates unobserved execution and creates a second timing authority. Backend sampling keeps the actual spawn boundary, effective timeout and observer delay distinguishable. Local measurement (`benchmarks/execution_clock.py`, Python 3.12.13, 100,000 samples × five): sampling/update median 921.4 ns; all three compact numeric fields add 79 bytes to a compact progress-event JSON envelope. This is producer overhead, not a bound on model/subprocess time. The command's 1 Hz browser ticker and DOM scan are removed; watcher scheduling countdowns retain their existing owner.

## Unified result surfaces and format policy

**Problem:** canonical capture, model delivery and frontend metadata existed, but debugging required separate source searches. Reports were often JSON strings containing escaped newlines; a field mismatch in async completion cards could hide the report while the model received it. **Decision:** `contracts/tool_result_surfaces_v1.json` owns result source precedence, model/frontend format policy, notification report tags, and bounded audit limits. `scripts/gen_tool_result_surfaces.py` derives the Python and TypeScript policy; `npm run check:tool-result-surfaces` gates drift. Unknown built-in, custom and MCP tools retain their producer output by default. `lib/tools/result_surfaces.py` exposes `inspect_tool_round`, `inspect_swarm_notification`, and `inspect_result_document` over existing Turn projections. No parallel result log, result table, transport, or replay owner is introduced. Frontend source resolution uses the same generated contract and honors explicit empty/null results; secondary `results` metadata never becomes proof of model delivery. The observer reports renderer inputs, not mounted HTML or proof of provider consumption. Repeated call IDs retain their Turn indices; unread history, missing evidence, deferred previews and diagnostic truncation remain explicit. Artifact reads use existing owner-scoped continuation APIs.

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

**Search observation decision:** Problem—wrong-topic snippets, circuit-open engines and missing article tables could look like successful research. Decision—`tofu-search` 0.11 separates fetch admission/ranking from observation quality, enforces explicit site scopes before any fetch, retains at most three uncertain snippet candidates in diagnostics, and reports failed/unavailable engines even with nonempty results. The full bounded uncertain set enters the existing result artifact, preserving follow-up recovery without flooding the preview or creating another store. Received DOM tables and omitted copy identifiers supplement article extraction. `lib/search/search_extract_contract.py::EXTRACTION_SCHEMA` owns the helper response: findings require exact inspected-source quotes, deterministic source offsets, and separate unresolved requests. The bounded single helper pass retains original source; invalid/ungrounded output returns raw evidence through the existing fail-soft handler. Full received text and exact quotes prove neither page/website completeness nor semantic entailment. Alternatives rejected: zero-overlap hard rejection loses aliases/languages; treating fetch success as relevance invents verification; another adjudicator model increases cost without recovering lost source. Package/host tests exercise degraded engines, scope violations, late facts, missing fields, false citations and raw-source fallback.

**Automatic evidence admission — Problem:** a generic one-word match could stand in for a substantive query, fetched text could silently upgrade that match, and one-hop links could escape the original site scope. **Decision:** subject terms exclude search operators; partial lexical matches remain explicitly uncertain. Reassess received article text without counting outbound link lists as subject evidence. Bounded recovery uses the original query and freshness when no useful candidates arrive, even if an engine returns unrelated rows; explicit engine selection remains authoritative. Site scopes apply to initial rows and deeper links. Deep crawling uses the remaining search deadline, and uncertain parents cannot amplify junk. Model output preserves source identity, available dates/ranks, uncertainty and coverage; snippets and page excerpts share an explicit presentation budget when one is supplied. Tofu host search automatically uses one 20,000-character content budget across the whole tool call, including batch queries; metadata and vertical results remain visible. The shared presentation owner captures complete received results, uncertain candidates, diagnostics and vertical data before selecting any preview. Serial and streaming cache paths preserve that canonical capture for artifact continuation. Direct-fetch behavior remains unchanged. **Alternatives rejected:** making Deep fetch more junk does not improve precision; strict all-keyword rejection loses valid aliases and multilingual recall; silently broadening queries breaks constraints; buying a new provider or weakening TLS/SSRF is neither necessary nor authorized. This adds no settings, cache or resident pool.

Offline degraded-SERP measurement (20 runs, mocked engine/fetch, no model or
network): 20 generic candidates incurred 20 page fetches before admission changes
and 3 afterward (85% fewer), with no junk promoted into ranked evidence. Median
local pipeline time was 4.750 ms before and 12.482 ms after: explainable evidence
checks add CPU work; this is not a demonstrated end-to-end latency improvement.
A separate 3,000-character payload fixture reduced bounded snippet-only output
from 100,113 to 4,629 total characters; source identity and metadata stay outside
the payload budget. No new resident concurrency or cache is introduced.

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
## Declared shell semantics

Problem: request-owned terminal tools were classified as confirmed writes solely
because their concurrency partition was writable, hiding no-op execution loops.
Decision: `ToolContractV2.shell_command_argument` names a declared string argument;
its private document field is `shellCommandArgument`. Custom-tool callers set
`execution.shell_command_argument`; validation and compilation preserve it through
both explicit and registry catalogs. This metadata grants no execution authority.
The generic progress guard treats these tools like built-in shells: observation,
opaque execution, and confirmed file mutation remain distinct. Exit receipts accept
the built-in terminal marker or a leading `exit_code=N` line. Changed outcomes reset
failure streaks. Alternatives rejected: name/description guessing and declaring all
successful shell calls to be mutations. Neither survives tool renaming or wrappers.


Canonical artifact traversal, signed continuation and query lifecycle follow
[Tool result queries](TOOL_RESULT_QUERY.md). <!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
