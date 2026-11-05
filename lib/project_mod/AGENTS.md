# Project modification guidance

## Scope

This package owns model-facing project reads, writes, and bounded command
execution. Tool schemas/approval live in `lib/tools/`; project coordination and
Git publication have separate owners.

## Editing rules

- Resolve every target beneath an explicit authorized project root. Reject
  traversal, unsafe symlinks, ambiguous roots, and path changes between check
  and use.
- Reads and writes carry project/root attribution and freshness evidence. A
  stale snapshot or mismatched write set fails visibly instead of overwriting
  concurrent user work.
- Prefer atomic replacement and recoverable file history. Multi-file changes
  define rollback behavior; never broaden a requested deletion or cleanup.
- Command execution uses structured arguments where possible and explicit cwd,
  environment, timeout, output, process-tree, and cancellation bounds.
- Schema validation and model intent are not authorization. Preserve approval
  receipts and deny unattended side effects that require user consent.
- Redact secrets and bound diffs, file content, command output, artifacts, and
  retained history.

`write_intents.py` owns bounded prepared-write evidence under
`contracts/project_write_intent_v1.schema.json`; `write_recovery.py` reconciles
it on project access and before tracked writes. `modifications.py` owns the
committed undo index; `undo_payloads.py` owns immutable packed content and
selection-time hydration under `contracts/project_undo_payload_ref_v1.schema.json`. Both use `session_lock.py` before the cache lock.
`undo_payloads.reclaim_orphans` runs once per process per session and again
when write recovery reconciles intent evidence ("Crash-artifact sweep"); never
put the payload directory scan back on a per-load or per-write path.
Read `docs/PROJECT_FILE_UNDO.md` for acknowledgement and conflict rules.

`modifications.py` commits one record by appending a line to
`modifications.journal`; it folds the whole index only for an oversized record,
a bounded log, or a whole-history replacement (undo). Admission is per project
journal, so its cost must stay proportional to the edit, not to the history: a
write validates the journal by identity plus a validated append offset instead
of re-materializing the index, and any caller that does materialize it (undo,
projections, a fold) hands those rows to that same admission rather than making
the commit re-read them. Substituting `_cache_get` for `_assert_journal_ready`
in an admission path is what makes a large journal stall unrelated edits.
The admission orders writers only (owner decision 2026-11-01): history
projections read through `_metadata_history`, which takes the admission only
to reconcile existing intent evidence, and a `run_command` round reports the
changes it recorded instead of re-reading history. Never wrap a read-only
projection in `_with_recovered_session`, `locked_session` or `_cache_get`; only
content hydration, undo and project open read under admission. Verify
`test_project_journal_read_admission.py`.
`docs/PROJECT_FILE_UNDO.md` owns the crash and ordering rules plus the
measurement. Verify `test_project_journal_append.py` with the contention,
payload and post-commit neighbors.

`workspace_lease.py` owns cooperative checkout/file exclusion and descriptor
lifetime for Tofu's own whole-workspace operations (delivery, integration
snapshots, checkpoints, dependency setup, Git maintenance), not authorization.
Model tool calls take no workspace lease (owner decision 2026-10-28): never add
lease admission, claim derivation or admission waits to `run_command`, the write
tools, MCP local paths or project dispatch. `git_guard.py` is the only workspace
protection for tool calls; `file_edit_mutex.py` keeps one write call's
read-modify-write atomic per resolved path and is never held across commands or
calls. See "Tool calls take no workspace leases" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`; verify
`test_tool_calls_take_no_workspace_lease.py`, `test_file_scoped_write_lease.py`
and the `test_workspace_lease*.py` suites.

`command_network_probe.py` owns the bounded complete HTTP-probe plan and the
curl/wget transfer plan (`prepare_file_transfers`) that execution uses;
first-option config suppression stays execution-only. See
`docs/runbooks/GIT_WORKSPACE_GUARD.md` and verify
`tests/project_mod/test_network_probe_lease.py`. It grants no authorization.

`process_group.py` owns bounded POSIX descendant retirement for foreground and
recovered commands. Its `descendant_pids` is the one owned-subtree discovery
(stdin sensors, search cleanup, activity supervision): it reads per-thread
kernel child lists and scans `/proc` only on a kernel without them, so never add
a per-caller whole-host scan ("Command stdin sensor discovery" in
`docs/RESOURCE_CONTROL.md`; verify `tests/tasks_pkg/test_run_command_stdin_probe.py`).
Leader exit is not group quiescence; cancellation must
await inherited descriptor closure. Retirement never replaces a command's
decided result: `run_command` stops what a finished command left in its group
and turns an unconfirmed retirement into a typed warning that keeps the record
("A finished command's result survives its group's retirement" in
`docs/runbooks/GIT_WORKSPACE_GUARD.md`). Recovered records verify PID starttime
and the observed group before signalling. No arbitrary recorded PID is authority.
Use `post_commit.defer_publication` outside the edit mutex and lease scopes so
optional callbacks run after freshness and file release. Coordination decisions live in
`docs/modules/project_integration.md`, not a duplicate policy in callers.

`foreground_command_records.py` records each foreground command group (owner
incarnation, leader start time, tool-call id) until it retires or exits; a failed
retirement keeps the record. The boot step retires a dead owner's groups before
task and swarm recovery, and swarm recovery refuses to resume an interrupted round
whose record survives. Background commands keep their own records. Decision:
"Interrupted tool rounds resume their own agent" in `docs/RUN_CONTINUITY.md`;
verify `tests/project_mod/test_foreground_command_records.py`.

`foreground_command_slots.py` owns the per-task registry behind the task slots
that address one foreground command (`_subprocess_pid`/`_subprocess_pgid`
mirror, pid-stamped `_cmd_interrupt`, `_cmd_handoff_control`). Both runners
always run against a `CommandTaskView`, on every execution path, so concurrent
commands of one task stay individually interruptible, watched and stoppable;
never write those slots on a task directly. Decision: "Per-command slots on
every execution path" in `docs/contracts/TOOL_EXECUTION_POLICY.md`; verify
section 7 of `tests/tasks_pkg/test_run_command_interrupt.py` and
`tests/tasks_pkg/test_run_command_background_handoff.py`.

`activity_supervised_process.py` runs Git whose duration follows the volume
(the integration snapshot's whole-tree commands) without a total deadline: a
fixed stall window renews whenever any thread of its process tree executes,
and retirement reuses `process_group`. Never add a wall-clock refusal there.
Verify `test_activity_supervised_process.py` and
`tests/storage/test_integration_snapshot_liveness.py`.

`post_commit.py` owns process-local ordered presentation notifications after
journal release; it is not a cross-process durable-history fence. File-history
staging remains under session admission. Cache identity/byte bounds stay in
`modifications.py`; authority is always read before direct file publication.

`write_tools/` search/replace matching is a ladder: exact → CRLF →
trailing-whitespace → unicode-escape → uniform-indent (relative-indent
re-framing) → unicode+indent, each rung ambiguity-guarded with located
errors; a miss returns the closest block to re-anchor from. Indent fallback
requires one uniform literal prefix shift, never independently stripped nesting.
`edit_file` defaults to whole-batch atomic preflight; only explicit `atomic=false`
permits partial application. It plans under its edit mutexes and takes journal
admission only to publish; report-only checks run after release.
`session_lock.py` admits a process's waiters in arrival order, and a
`journal-busy` refusal names the holding tool call and its hold time; verify
`test_project_journal_admission_order.py`. `apply_diffs` retains its default per-edit failure
isolation and accepts `atomic=true`. Both group one undo transition per physical
file; neither promises cross-file rollback after later I/O failures. Read
`docs/PROJECT_FILE_UNDO.md` for the preflight and applied-failure contract.

`_finalize_insertion` owns insert echo checks shared by local and desktop
writers. See `docs/modules/tools_execution.md` for the content-preservation
boundary; verify `test_insert_structural_overlap.py` with the unified-edit,
atomic-preflight and error-kind tests before changing it.

`git_command_scope.py` is the single Git target resolver for approval and the
Git guard, including shell cwd control, prospective worktrees and Git metadata
redirects. `command_analysis.py` owns risk/read-effect classification;
`git_inspection_execution.py` prepares shell and internal argv reads without
refreshing the writer index, replaying writes or changing config/path results.
Porcelain diff/dirty describe require private indexes even on modern Git.
`git_index_snapshot_exec.py` snapshots immediately before each shell query so
preceding writes remain visible; preparation owns cleanup after process retirement.
Preparation spawns the copy of that child pinned at its own import, never the
checkout file; the child refuses another argv shape with restart guidance. Read
"Long-running processes spawn helpers from their loaded source" in
`docs/DEFENSIVE_PATTERNS.md` before adding any by-path helper spawn.
New Git shapes extend these owners,
not a new parser in dispatch/runner. Verify `test_git_command_scope.py`,
`test_git_inspection_execution.py` and command/guard concurrency suites.

`command_structure.py` is the bounded shell-structure parser the
catastrophic-delete guard (``_catastrophic_delete_detail`` / ``_is_catastrophic_delete``
in `command_analysis.py`) and the dangerous-command guard (``dangerous_command_rule``,
the one judge of run_command, the desktop agent and the safety pre-hook) are built
on: it delimits simple commands, ``( … )`` groups, substitution bodies, heredocs,
``case`` clauses, ``[[ … ]]`` tests, arithmetic and function definitions the way
a shell does, and raises ``ShellStructureUnknown`` for what it cannot delimit.
Heredoc bodies, quoted strings and Git pathspec magic stay data; a shell's stdin
heredoc or ``-c`` string is judged as commands. Both guards read programs through
``_program_views`` (assignments, wrappers, ``xargs``, ``eval`` and the runners of
``_RUNNER_GRAMMARS``: ``ssh``, ``su -c``, ``find -exec``, ``docker exec`` …); add a
runner there with its documented option grammar, never a second view. sudo/doas
options follow ``_PRIVILEGE_GRAMMARS``, which ``_unwrap_command_parts`` shares. Never reintroduce an ad-hoc string splitter or a
raw-text match for delete-target or dangerous-command judgement, and never turn
a parse failure or an unknown wrapper/runner option into a refusal unless the
text could run rm/rmdir/unlink or matches `DANGEROUS_PATTERNS`. Verify `tests/project_mod/test_delete_guard.py`,
`tests/project_mod/test_delete_guard_structure.py` and
`tests/project_mod/test_dangerous_command_structure.py`.

`git_guard.py` owns the shared-checkout Git guard: ack-nonce lifecycle,
refusal text, and the read-only mergedness probe that downgrades
`git branch -D` on provably merged branches for tree-destroying Git commands
intercepted by the tool pipeline. Classification stays pure in
`command_analysis.py` (plain `branch -d` is self-guarding `safe_write`);
presence and probe subprocesses are only read. Read
`docs/modules/project_integration.md` for the coordination contract.

`task_delta_delivery.py` owns automatic task-only three-way workspace merging/publication and proven-recoverable clone
reclamation; `workspace_runtime.py` owns lazy reconstructible dependency bridges. Durable intent,
revocation and recovery stay in `delivery.py`; gates/snapshots stay in `integration_control.py`.
`delivery_retry.py` owns condition fingerprints, bounded conflict evidence and immutable scoped
repair proposals under the workspace delivery retry/repair schemas. Verify `test_continuous_delivery.py`.
The resume pass reads only `integration.workspace.delivery_action_page` rows (complete rows
whose receipt asks for action) and runs only when due: start/error recovery, a committed
`DELIVERY_STATE_OPERATIONS` hint, or rows left by the last pass. Never reintroduce a
per-tick history scan or a per-row reread; a new receipt state that needs action changes
both authorities' filter. Decision "Delivery resume demand" in
`docs/modules/project_integration.md`; verify `test_delivery_resume_demand.py`.

`managed_repository.py` prepares a plain folder or empty repository for isolated workers
so people never run Git: one baseline commit, a content policy in `.git/tofu-managed.json`
rendered into `.git/info/exclude`, and a create-only default `AGENTS.md`
(`managed_repository_guidance.py`). Repositories with their own history are never changed.
Bulky data is shared through links in worker checkouts and never enters a snapshot. The
decision record is "Worker workspace preflight" in `docs/PROJECT_COORDINATION.md`; verify
`test_managed_repository.py`, `test_worker_workspace_refusal.py` and the plain-folder
file-lifecycle and reclamation tests.

`tree_index.py` owns coalesced root builds and directory scans; `read_tools.py`
owns index-search/count chunks. Both use the shared demand executor with idle
reuse hints, never active count quotas. Incomplete index candidates stay out of
the cache; index search failure uses the live backend. Verify tree-index and
grep-withheld tests plus the demand/cleanup boundary in the tools domain map.

`tree_index_storage.py` owns automatic local index placement and evict-only
blob retention, using `serverctl_pkg/local_placement.py`. Capacity failure
skips persistence, never truncates index contents or denies file work. See
`docs/INDEX_FASTPATH.md` and `tests/project_mod/test_tree_index_storage.py`.

`content_shadow.py` owns demand-filled read-only search copies and cross-process
cache leases. `read_tools.py` retains complete search, paging and error behavior.
Keep source freshness checks, live retry, original deadlines and exact filename
rewriting; never turn a cache-size limit into a candidate/work limit. See
`docs/INDEX_FASTPATH.md` and `test_content_shadow.py`.

## Verification

`receipts.py` owns optional device write envelopes defined by
`contracts/project_file_receipts_v1.schema.json`; verify with
`tests/project_mod/test_project_file_receipts.py` and the remote paper browser journey.

Run focused project read/write/command tests, then atomicity, rollback, root
attribution, freshness, symlink/traversal, approval, cancellation, and concurrent
writer tests.

`file_read_probe.py` adapts explicitly admitted argv/environment to the shared
`run_command_process` lifecycle, preserving output separately from optional kernel
read facts. It is not a model tool or approval port; callers retain admission.
Optional descriptor arguments bind selected inputs without freezing contents;
borrowed FDs stay caller-owned. Verify observed-process, pinned-input and kernel
read probes under `tests/evolution/`.

`run_command.frozen_input_paths` selects owned protocol-derived read observations
only after ordinary command/environment/network/sandbox preparation. It closes
stdin and preserves originals; verify agent-read-selection and real-measurement
tests. This does not grant semantic input-use or timing benchmark proof.

`tree_index.py` walks complete authorized trees with windowed directory Futures.
Entry retention targets cannot truncate builds, reject persisted indexes, or
prevent write-fresh checkpoints. Deep trees and expired legacy walk deadlines
remain complete; actual read failures never install a partial index. Verify
`test_tree_index.py` and `test_tree_index_scan_pressure.py`.

Project grep searches complete source files, including those above the retired
2 MiB default. `grep_candidates` sizes are scheduling observations. Python find
has no private entry-count cutoff; result/time policies remain with search.

Batched read/grep/find operations execute every entry and retain every receipt.
Presentation quotas belong to the shared result envelope, never batch slicing
or early loop termination. Verify `test_project_batch_demand.py`, range,
projection, source-recovery and settlement neighbors.

Whole plain-text reads retain their complete source regardless of size or
data-file heuristics. Explicit ranges remain exact; presentation belongs to
the result envelope. `MAX_FILE_SIZE` still belongs to modification capture,
not reader admission. Verify `test_project_file_demand.py` and range/facade
contracts before changing that independent history owner.

Edit batches process every requested operation. Count thresholds cannot cut off
preflight, hide late failures, or remove result lines. Preserve grouped physical
writes/undo and whole-batch atomic preflight; verify `test_project_edit_demand.py`
with batch atomicity, write-failure and recovery neighbors.

Write intents preserve complete byte evidence and discover all pending records;
legacy recovery-budget values cannot hide durable state. Identity, digest,
regular-file, corruption and real I/O guards remain mandatory. The physical
extent compatibility helper is not an admission knob. Verify write-intent,
crash-recovery, byte-roundtrip and grouped-undo tests.

`delivery_acceptance.py` binds actual immutable-candidate checks to source and
checker revisions. The existing workspace origin retains one bounded receipt;
missing checks never inherit task completion. Read `docs/WORKSPACE_ACCEPTANCE.md`;
verify the SQLite/native acceptance tests and built-app integration browser test.

`effect_reconciliation.py` binds tracked local write publication to the common
execution effect and reads existing undo/intent evidence without replay. Read
`docs/contracts/TOOL_EFFECT_RECOVERY.md`; verify file-effect reconciliation,
selected-store restart, and owner-scoped inspection tests.
