# HOT_PATH — called once per stream round to sanitize any malformed
# tool_call.arguments payload the model just emitted.
"""Sanitize malformed ``tool_call.arguments`` JSON before the next
gateway roundtrip.

Extracted 2026-07-31 ( slice 14) from
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
    Walk every ``parsed_tcs`` occurrence in order and claim its matching live
    ``tool_calls[i]`` on ``messages[-1]`` by shared object identity, with a
    FIFO-per-ID compatibility fallback. Good entries consume their occurrence
    too, so a duplicate provider ID cannot redirect a later malformed repair
    onto its first sibling. Then overwrite only that occurrence's
    ``function.arguments`` to ``'{}'`` — but ONLY when the raw text is not
    already a valid JSON object: a contract-level rejection keeps its payload
    so the recovery round sees the exact arguments the schema error refers
    to. The error
    ``tool_result`` still teaches the model what went wrong; the
    gateway now sees valid JSON on the next round.

    The RAW bad args (truncated at 600 chars) are kept on an INFO
    log line so 2026-07-27 concatenated-tool-name postmortems still
    have the decisive evidence.
"""

from __future__ import annotations

from collections import defaultdict, deque
import json
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
    last_msg = messages[-1] if messages else {}
    live_calls = [
        call for call in (last_msg.get('tool_calls', []) or [])
        if isinstance(call, dict)
    ] if isinstance(last_msg, dict) else []
    live_object_ids = {id(call) for call in live_calls}
    live_by_id: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for call in live_calls:
        call_id = str(call.get('id') or '')
        if call_id:
            live_by_id[call_id].append(call)
    claimed_live_objects: set[int] = set()

    for tc, fn_name, tc_id, fn_args, rn, round_entry, args_parse_err in parsed_tcs:
        # Parse preserves the live dict object. The occurrence queue is a
        # compatibility fallback for tests/adapters that reconstruct tuples.
        # Consume a target for EVERY parsed entry (including good ones), so a
        # later malformed occurrence with the same provider id cannot rewrite
        # the first sibling.
        live_tc = None
        if isinstance(tc, dict) and id(tc) in live_object_ids \
                and id(tc) not in claimed_live_objects:
            live_tc = tc
        else:
            queue = live_by_id.get(str(tc_id or ''))
            while queue and id(queue[0]) in claimed_live_objects:
                queue.popleft()
            if queue:
                live_tc = queue.popleft()
        if live_tc is not None:
            claimed_live_objects.add(id(live_tc))
        if not args_parse_err:
            continue
        if live_tc is None:
            logger.warning(
                '[%s] conv=%s Could not occurrence-pair malformed tool args '
                'for tool=%s tc_id=%s; refusing to guess a live call',
                tid, conv_id, fn_name, str(tc_id or '')[:12])
            continue
        fn = live_tc.get('function') or {}
        if not isinstance(fn, dict):
            continue
        bad_args = fn.get('arguments', '')
        # A contract/shape rejection means the arguments PARSED fine — they
        # are valid JSON whose content failed validation. Preserve them:
        # the next gateway round accepts valid JSON, and the recovery round
        # needs the exact payload the fed-back schema error refers to
        # (rewriting it to '{}' hid the evidence; conv mtdqz4bkuyitzj).
        # Only text that is NOT a valid JSON object can trip the gateway's
        # arguments-JSON gate (HTTP 400 invalid function arguments json).
        if isinstance(bad_args, dict):
            continue
        if isinstance(bad_args, str):
            try:
                if isinstance(json.loads(bad_args), dict):
                    logger.info(
                        '[%s] conv=%s Keeping valid-JSON tool_call args for '
                        'tool=%s tc_id=%s (%d chars) — rejection was '
                        'contract-level; payload preserved as evidence for '
                        'the recovery round',
                        tid, conv_id, fn_name, str(tc_id or '')[:12],
                        len(bad_args))
                    continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        fn['arguments'] = '{}'
        logger.info(
            '[%s] conv=%s Sanitized malformed tool_call args for '
            'tool=%s tc_id=%s (was %d chars) — error fed back to '
            'model in matching tool_result; gateway sees valid JSON',
            tid, conv_id, fn_name, str(tc_id or '')[:12],
            len(bad_args) if isinstance(bad_args, str) else 0)
        # Keep the RAW args text, not just its length. It is the
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
                tid, conv_id, str(tc_id or '')[:12],
                model, bad_args[:600],
                '…(truncated)' if len(bad_args) > 600 else '')
