# HOT_PATH — called once per stream round to sanitize any malformed
# tool_call.arguments payload the model just emitted.
"""Sanitize malformed ``tool_call.arguments`` JSON before the next
gateway roundtrip.

Extracted 2026-07-31 (pt_03f4cdf1 slice 14) from
``lib/tasks_pkg/orchestrator/_run.py``'s stream loop.

**Why this exists**
    When a model emits ``tool_calls=[{arguments: '...'}]`` where
    ``arguments`` is invalid JSON (common with weaker models that
    mis-escape backslashes in regex args, e.g. ``\\d`` instead of
    ``\\\\d``), ``parse_tool_calls`` catches the JSONDecodeError and
    builds an error tool_result. But the assistant message we already
    appended still contains the RAW bad ``arguments`` string.

    On the next round, the orchestrator replays
    ``assistant(tool_calls=[..bad args..]) + tool(error_msg)`` to the
    upstream gateway, which validates the JSON-string itself and
    rejects with HTTP 400 ``invalid function arguments json string``.
    The whole conversation gets stuck — the model never sees the
    error tool_result, can't recover, task ends in
    ``finishReason=error``.

**Fix**
    Walk ``parsed_tcs`` and, for any tc with a non-None
    ``args_parse_err``, find the matching live ``tool_calls[i]`` on
    ``messages[-1]`` (matched by tc id, not position — a round may
    have several parallel tool_calls where only one is malformed) and
    overwrite its ``function.arguments`` to ``'{}'``. The error
    ``tool_result`` still teaches the model what went wrong; the
    gateway now sees valid JSON on the next round.

    The RAW bad args (truncated at 600 chars) are kept on an INFO
    log line so 2026-07-27 concatenated-tool-name postmortems still
    have the decisive evidence.
"""

from __future__ import annotations

from typing import Any, Iterable

from lib.log import get_logger


logger = get_logger(__name__)


def sanitize_malformed_tool_call_args(
    parsed_tcs: Iterable[tuple],
    messages: list[dict[str, Any]],
    *,
    tid: str,
    conv_id: str,
    model: str,
) -> None:
    """Rewrite malformed ``tool_call.arguments`` to ``'{}'`` in place.

    ``parsed_tcs`` is the 7-tuple sequence returned by
    ``parse_tool_calls``: ``(tc, fn_name, tc_id, fn_args, rn,
    round_entry, args_parse_err)``. Only entries with a truthy
    ``args_parse_err`` are acted on; the rest are ignored.

    ``messages`` is the live message list; we mutate the last message
    (the assistant one we just appended) in place. Called with an
    empty list, no matching id, or empty ``parsed_tcs``, the call is
    a silent no-op.

    ``tid`` / ``conv_id`` / ``model`` are diagnostic scalars stamped
    onto the two INFO log lines.
    """
    for tc, fn_name, tc_id, fn_args, rn, round_entry, args_parse_err in parsed_tcs:
        if not args_parse_err:
            continue
        last_msg = messages[-1] if messages else {}
        for live_tc in last_msg.get('tool_calls', []) or []:
            if live_tc.get('id') != tc_id:
                continue
            fn = live_tc.get('function') or {}
            bad_args = fn.get('arguments', '')
            fn['arguments'] = '{}'
            logger.info(
                '[%s] conv=%s Sanitized malformed tool_call args for '
                'tool=%s tc_id=%s (was %d chars) — error fed back to '
                'model in matching tool_result; gateway sees valid JSON',
                tid, conv_id, fn_name, tc_id[:12],
                len(bad_args) if isinstance(bad_args, str) else 0)
            # ★ Keep the RAW args text, not just its length. It is the
            #   decisive cross-check on how a malformed call was
            #   produced (2026-07-27 concatenated-tool-name inquiry):
            #     * two concatenated valid JSON objects (``{...}{...}``)
            #       ⇒ two calls merged into one slot by the SSE
            #       accumulator (see the tool_calls-shape observation
            #       logs in lib/llm/_sse_core.py)
            #     * one single malformed object ⇒ model-side output,
            #       nothing for us to fix in parsing
            #   Truncated: args carry user/file content, and this line
            #   is INFO on a hot path.
            if isinstance(bad_args, str) and bad_args:
                logger.info(
                    '[%s] conv=%s   ↳ raw malformed args for tc_id=%s '
                    'model=%s: %r%s',
                    tid, conv_id, tc_id[:12],
                    model, bad_args[:600],
                    '…(truncated)' if len(bad_args) > 600 else '')
            break
