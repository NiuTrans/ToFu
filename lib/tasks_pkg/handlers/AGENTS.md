# Task tool-handler guidance

## Scope

Handlers adapt unified tool calls to domain services. Tool schemas, visibility,
approval, and result envelopes live in `lib/tools/`; domain policy remains with
the invoked package.

`project.py` owns admission order, transport choice and final settlement.
`_project_execution.py` runs admitted local I/O and command callbacks;
`_project_presentation.py` projects images and promotes guarded write artifacts.
Both command entry points reuse `code_exec.finalize_command_output`, including
failed execution. Verify project freshness/refusal, working-file witnesses,
background handoff, image/artifact and command-output suites when moving phases.

Read tools streamed during the model response are pre-executed by
`../streaming_tool_executor.StreamingToolAccumulator._execute_one` and served
as prefetch hits, so `_handle_project_tool` never runs for them. Mirror every
new pre-execution side effect of read tools (freshness tokens, guidance
acquisition, timing anchors) there, in the same order and with the same
exemptions; `tests/tasks_pkg/test_project_guidance_streamed_reads.py` drives
both paths.

## Editing rules

- Parse the registered schema once, recover explicit owner/task/project context,
  and call one public domain entry point. Do not duplicate validation or policy.
- Enforce approval/capability receipts before side effects and retain exact root,
  browser device, remote device, or network attribution.
- Settle the canonical result exactly once on success, typed failure, timeout,
  cancellation, or partial artifact output.
- Propagate cancellation to subprocess, network, browser, MCP, child-task, and
  storage work. Clean only resources owned by the call.
- Bound input expansion, output, artifacts, progress events,
  retries, and diagnostics; redact secrets before results/events.
- Keep display formatting outside execution semantics and never write directly
  to route/frontend state.

## Verification

Run the focused handler/domain tests, then unified gateway, approval, isolation,
settle-all-lanes, wire-shape, and cancellation tests.

`_artifact_page_budget.py` plans only smaller query limits using the shared model
projection counter. `tool_result_artifacts.py` re-queries the original authority
cursor and measures the returned page; planning never publishes a cursor or page.
Verify Artifact refit planning, batch demand, and real-backend restart suites;
semantics and resource evidence live in `docs/contracts/TOOL_RESULT_QUERY.md`
and `docs/RESOURCE_CONTROL.md`.
Artifact source/refitting boundaries borrow the existing task/tool cancellation
signal and unwind through common executor settlement. Verify query cancellation,
native gateway propagation and both backends before changing those boundaries.

`_mcp_files.py` binds declared file-write receipts before MCP settlement;
verify with `tests/tasks_pkg/test_overleaf_write_receipts.py` and MCP wrapper neighbors.

`../working_file_sources.py` owns task-local host witnesses for opt-in file reads.
Both generic and file-scoped project handlers publish only after the common
successful terminal boundary; working-fact admission rechecks current scope.
No raw tool JSON grants filesystem authority. Verify native gateway, cancellation,
root replacement and terminal receipt release in the working-file-source suites.
