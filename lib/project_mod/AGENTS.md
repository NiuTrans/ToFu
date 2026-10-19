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
Read `docs/PROJECT_FILE_UNDO.md` for acknowledgement and conflict rules.

`workspace_lease.py` owns cooperative checkout/file exclusion and descriptor
lifetime, not authorization. `run_command.py` owns command write-scope admission;
any heredoc admission view must preserve the original execution/approval source.

`command_network_probe.py` owns the bounded complete HTTP-probe plan used by
both writer admission and execution; first-option curl config suppression stays
execution-only. See `docs/runbooks/GIT_WORKSPACE_GUARD.md` and verify
`tests/project_mod/test_network_probe_lease.py`. It grants no authorization.

`process_group.py` owns bounded POSIX descendant retirement for foreground and
recovered commands. Leader exit is not group quiescence; cancellation must
await inherited descriptor closure. Recovered records verify PID starttime
and the observed group before signalling. No arbitrary recorded PID is authority.
Use `post_commit.defer_publication` outside filesystem lease scopes so optional
callbacks run after freshness and file release. Coordination decisions live in
`docs/modules/project_integration.md`, not a duplicate policy in callers.

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
permits partial application. `apply_diffs` retains its default per-edit failure
isolation and accepts `atomic=true`. Both group one undo transition per physical
file; neither promises cross-file rollback after later I/O failures. Read
`docs/PROJECT_FILE_UNDO.md` for the preflight and applied-failure contract.

`git_command_scope.py` is the single Git target resolver for approval and writer
admission, including shell cwd control, prospective worktrees and Git metadata
redirects. `command_analysis.py` owns risk/read-effect classification;
`git_inspection_execution.py` prepares legacy Git reads without replaying writes
or changing config/path query results. New Git shapes extend these owners,
not a new parser in dispatch/runner. Verify `test_git_command_scope.py`,
`test_git_inspection_execution.py` and command/guard concurrency suites.

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

`tree_index.py` owns coalesced root builds and directory scans; `read_tools.py`
owns index-search/count chunks. Both use the shared demand executor with idle
reuse hints, never active count quotas. Incomplete index candidates stay out of
the cache; index search failure uses the live backend. Verify tree-index and
grep-withheld tests plus the demand/cleanup boundary in the tools domain map.

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
