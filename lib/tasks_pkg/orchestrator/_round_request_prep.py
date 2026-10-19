"""Round-request preamble ( slice 28).

Extracted 2026-07-31 from ``lib/tasks_pkg/orchestrator/_run.py``
run_task's stream loop, where the cluster ran inline once per stream
round after inbox drain and before the streaming-tool accumulator
construction. Byte-identical behaviour.

Four steps, in order:

1. Reuse the turn's stable wire list, refreshing bounded root MCP execution
   authority through ordinary policy assembly before streaming ingestion.
2. Build the request body through the explicit ``_ports`` dependency owner.
3. Emit the messages-snapshot debug event from that canonical body, avoiding a
   second full-history sanitize; body-build failures retain the old diagnostic
   fallback from the untouched source messages.
4. Attach validated prompt-admission evidence and internal task/conversation
   context — the session-stable TTL latch, Responses prompt-cache namespace,
   and user-approved economic working-set threshold. Protocol converters
   consume these keys; none reach an upstream wire unchanged.

Returns ``(_tools_this_round, body)``: the gated tool list is still
needed downstream by the round-checkpoint call (slice 20).
"""

from __future__ import annotations

import hashlib

import lib.tasks_pkg.orchestrator._ports as orchestrator_ports
from lib.log import get_logger
from lib.tasks_pkg.orchestrator._messages_snapshot import (
    emit_messages_snapshot_event,
)
from lib.token_counter.evidence import (
    ADMITTED_INPUT_TOKENS_KEY,
    validated_admitted_input_tokens,
)


logger = get_logger(__name__)


def build_round_request(task, rs, messages, tool_list, *,
                        round_num, tid,
                        thinking_depth, temperature, max_tokens,
                        response_format, admitted_input_tokens=None,
                        admitted_tool_schema_tokens=None,
                        admitted_tool_schema_fingerprint=None,
                        reusable_text_token_counts_by_identity=None):
    """Build this round's (gated tool list, request body) pair.

    ``task`` / ``rs`` / ``messages`` / ``tool_list`` are positional
    carriers; every scalar is keyword-only so callers cannot get
    argument order wrong. ``rs`` supplies model / preset /
    thinking_enabled (mutated across rounds by resume-state and the
    LLM-call writeback, so read fresh each round).
    """
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import refresh_root_mcp_authority
    refresh_root_mcp_authority(task)
    _tools_this_round = tool_list
    from lib.tasks_pkg.model_images import pin_model_images, task_image_reuse
    pin_model_images(messages, model=rs.model, user_id=task.get('_userId'))

    # Reasoning-blowout recovery (set by _terminal_recovery after a round
    # that burned the whole completion budget on analysis with zero content
    # and zero tool calls): compile THIS round with thinking off — the
    # cheapest legal rung on cannot-disable models (GLM-5.3/Kimi K3),
    # disabled elsewhere. One-shot; the recovery budget bounds repeats.
    _thinking_this_round = rs.thinking_enabled
    if task.pop('_reasoning_blowout_degrade_next', False):
        _thinking_this_round = False
        from lib.log import get_logger as _get_logger
        _get_logger(__name__).info(
            '[Task %s] reasoning degraded for one round after '
            'reasoning-channel blowout (round %d)', tid, round_num)

    _snapshot_arguments = {
        'tid': tid,
        'round_num': round_num,
        'model': rs.model,
        'thinking_enabled': _thinking_this_round,
        'thinking_depth': thinking_depth,
        'preset': rs.preset,
        'temperature': temperature,
        'max_tokens': max_tokens,
        'response_format': response_format,
        'tools': _tools_this_round,
    }
    try:
        from lib.tasks_pkg.compaction._tokens import _get_context_limit
        budget_task = dict(task, config=dict(task.get('config') or {}, model=rs.model))
        body = orchestrator_ports.build_request_body(
            rs.model, messages,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking_enabled=_thinking_this_round,
            preset=rs.preset,
            thinking_depth=thinking_depth,

            # The task chooses intent; only a selected slot owns its dialect.
            defer_reasoning=True,
            context_window=_get_context_limit(budget_task),
            tools=_tools_this_round,
            response_format=response_format,
            stream=True,
            precomputed_input_tokens=admitted_input_tokens,
            image_reuse=task_image_reuse(task, model=rs.model),
            image_user_id=task.get('_userId'),
        )
    except Exception:
        # Retain the failed preflight in the inspector, but never replace the
        # body builder's typed failure with diagnostic work.
        emit_messages_snapshot_event(
            task, messages, **_snapshot_arguments)
        raise

    # The successful body already paid for field stripping, structural repair,
    # and canonical ordering. Reusing its message list avoids a second O(prompt)
    # sanitize/copy while the snapshot projector remains caller-independent.
    emit_messages_snapshot_event(
        task, messages,
        prepared_messages=body.get('messages'),
        **_snapshot_arguments,
    )
    # Preserve the already-paid full-prompt admission count for downstream
    # slot retries and cache-settle classification. Consumers fall back to
    # local estimation unless
    # this is a positive non-bool integer; provider boundaries strip the key.
    _validated_input_tokens = validated_admitted_input_tokens(
        admitted_input_tokens)
    if _validated_input_tokens is not None:
        body[ADMITTED_INPUT_TOKENS_KEY] = _validated_input_tokens
    # Attach task_id for session-stable TTL latch in
    # add_cache_breakpoints (prevents mid-session cache key shift).
    body['_task_id'] = task['id']
    body['_conv_id'] = task.get('convId') or ''
    # Private transport metadata for the durable Request Inspector archive.
    # ``prepare_request`` removes it at the same boundary as every other
    # underscore key, after using it to bind the final provider-specific body
    # to the owner/Attempt. It never reaches provider JSON.
    body['_raw_archive_context'] = {
        'userId': task.get('_userId') or 0,
        'conversationId': task.get('convId') or '',
        'turnId': task.get('_turnId') or '',
        'attemptId': task.get('_attemptId') or '',
        'taskId': task.get('id') or '',
        'roundNum': round_num + 1,
        'model': rs.model,
    }
    # Request-local authority latches must never survive a failed policy
    # lookup into the next round.
    from lib.tasks_pkg.orchestrator._tool_request_metadata import attach_tool_request_metadata
    attach_tool_request_metadata(task, body, messages, _tools_this_round,
        model=rs.model, round_num=round_num, tid=tid)
    # OpenAI recommends a stable, privacy-preserving end-user identifier.
    # Never send a raw tenant/user value; anonymous personal-mode tasks omit
    # the field rather than inventing a cross-user identity.
    _principal = (task.get('_userId')
                  or (task.get('config') or {}).get('user') or '')
    if _principal:
        body['_safety_identifier'] = 'tofu_' + hashlib.sha256(
            ('tofu-safety:' + str(_principal)).encode('utf-8')
        ).hexdigest()[:32]
    try:
        from lib.tasks_pkg.compaction._tokens import (
            _compaction_trigger_threshold,
            _working_set_token_limit,
        )
        _working_set = _working_set_token_limit(task)
        # A positive economic ceiling is additionally clamped by the model's
        # window-safety threshold.  Sending a 2M operator override verbatim to
        # a 1M Responses model would make server compaction useless past the
        # hard limit.  Zero remains an explicit opt-out.
        body['_working_set_tokens'] = (
            _compaction_trigger_threshold(task)[0] if _working_set > 0 else 0)
    except Exception as e:
        # Request construction must remain available if the optional economic
        # policy module cannot load; the local compaction gate logs its own
        # failures and the Responses converter treats a missing value as off.
        logger.warning('[Task %s] working-set policy lookup failed: %s',
                       tid, e, exc_info=True)

    try:
        from lib.context_telemetry import (
            TOOL_SCHEMA_EVIDENCE_KEY,
            build_tool_schema_evidence,
            capture_round_context,
        )
        # The opaque sidecar proves only this body-builder call. The existing
        # immutable wire-catalog copy is the later identity baseline, avoiding
        # another schema/list copy while provider projections remain free to
        # invalidate the count by replacing any element.
        if body.get('tools') is _tools_this_round:
            _schema_evidence = build_tool_schema_evidence(
                body.get('_tool_wire_catalog'),
                admitted_tool_schema_tokens,
                model=rs.model,
                source_fingerprint=admitted_tool_schema_fingerprint,
            )
            if _schema_evidence is not None:
                body[TOOL_SCHEMA_EVIDENCE_KEY] = _schema_evidence
        capture_round_context(
            task, body.get('messages') or [], _tools_this_round,
            round_num=round_num,
            model=rs.model,
            precomputed_tool_schema_tokens=admitted_tool_schema_tokens,
            reusable_text_token_counts_by_identity=(
                reusable_text_token_counts_by_identity),
        )
    except Exception as e:
        logger.debug('[Task %s] round context telemetry skipped: %s', tid, e)

    return _tools_this_round, body
