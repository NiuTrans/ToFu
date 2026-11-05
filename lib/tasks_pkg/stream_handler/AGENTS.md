# Stream recovery guidance

Read `docs/AGENT_LIFECYCLE.md` and `docs/modules/task_engine.md` for the owning
execution contract. `api.py` adapts `ProviderStreamResult` once and returns the
validated `RecoveryDecision`; legacy flags never override typed completion.

## Owners

- `_analyse.py` orders policy checks and owns normal program/commentary/output
  continuation. Keep cancellation and protocol recovery before retry buckets.
- `_transport_recovery.py` owns empty-stream retries, route diagnostics and
  partial-output rewind. Transport recovery never appends to `messages`: a cut
  attempt's output is discarded and the unchanged request is resent (no
  prefill, no continuation prompt; `docs/contracts/TURN_SETTLEMENT.md`).
- `_response_recovery.py` handles retained empty-stop, canned-greeting and
  gateway-limit responses after primary terminal and empty-transport recovery.
- `_tool_recovery.py` refuses missing/incomplete tool payloads before dispatch.
- `../round_retry.py` shares exact round-base text rollback with model-refusal
  fallback. The manager retains content epochs and event publication authority.
- `_budget.py` owns existing retry limits/backoff; `_terminal_recovery.py` keeps
  its distinct task-wide terminal-response recovery and replay suppression.

Recovery modules borrow task/result state; they do not dispatch, settle, persist,
or maintain a parallel retry registry. Provisional text discard and paid usage
are separate facts. Keep their existing order and
counter scopes explicit.

Verify incomplete-tool and missing-payload recovery, stream truncation, model
refusal fallback, retry envelopes, zero-byte pacing, and actual root-loop/replay
neighbors. Test emitted decisions, text epochs/history and diagnostic fields;
source strings or private locations are not behavioral proof.
