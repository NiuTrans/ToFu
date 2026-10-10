"""Single source of truth for the "what the model actually receives" message view.

The debug panel exists to diagnose context drift, so it MUST show the exact
OpenAI-form message array the LLM sees — not an earlier intermediate state.
Historically two paths fed the panel and disagreed:

  * COLD — ``GET /api/v1/conversations/<id>/debug-messages`` ran only
    ``build_api_messages_from_db`` (no system-context injection, no
    cache-reorder, no sanitization).
  * HOT — the live ``messages_snapshot`` SSE was captured in the orchestrator
    AFTER ``compose_task_context`` but before ``build_body`` sanitization.

Neither equalled the real outbound (wire) array. This module collapses both
onto one function so the panel is faithful.

Two public entry points
=======================
``apply_wire_sanitize(messages, *, conv_id='', provider_id='', user_id=None)``
    The model-agnostic, IO-free tail of ``build_body`` — the transforms that
    change the *OpenAI-form messages array* the model receives:
        _strip_non_api_fields → (gated) _sanitize_messages
        → _strip_empty_text_blocks → _fix_tool_call_wire_shape
        → _fix_orphaned_tool_calls → _drop_empty_assistant_messages
        → _merge_consecutive_same_role → _fix_empty_user_messages
    DELIBERATELY OMITS the transport-layer / provider-body steps
    (_validate_image_blocks disk I/O, _downscale_oversized_images Pillow,
    vision-strip, gemini/claude reasoning injection, provider-specific body
    fields) — those reshape the transport envelope, not the logical message
    list, and are surfaced to the user as "transport-layer transforms not
    expanded" rather than shown.

``build_wire_messages(raw_messages, config, *, user_id, mode, task=None, conv_id='', provider_id='')``
    The full COLD-path pipeline:
        _transform_messages → compose_task_context → apply_wire_sanitize
    ``mode='snapshot'`` (the endpoint) runs inject with a throwaway task and
    empty conv_id so it is side-effect-free and never pollutes the live
    request path's conv-keyed caches; per-round content (memory
    ``<relevant_memories>`` / date) is reconstructed as a hypothetical
    first-round, which the panel labels as an approximation.

Gateway-sanitization parity
===========================
``_sanitize_messages`` (the gateway-blocked-term replacement) is PROVIDER
GATED inside ``build_body`` exactly as::

    _pid.startswith('sankuai') or (not _pid and 'sankuai' in lib.LLM_BASE_URL)

The chat main loop builds its body with ``provider_id=''`` (orchestrator.py
:1533) and the pre-built-body dispatch branch never re-runs sanitization, so
the real outbound array's gateway step is decided by that auto-detect on
``LLM_BASE_URL``. We REPLICATE that gate verbatim here so the wire preview is
never MORE aggressive than the real request (an unconditional sanitize would
over-clean and lie in the other direction).
"""

from __future__ import annotations

import lib as _lib
from lib.llm_sanitize import (
    _drop_empty_assistant_messages,
    _fix_empty_user_messages,
    _fix_orphaned_tool_calls,
    _fix_tool_call_wire_shape,
    _merge_consecutive_same_role,
    _sanitize_messages,
    _strip_empty_text_blocks,
    _strip_non_api_fields,
)
from lib.log import get_logger

logger = get_logger(__name__)


def _gateway_sanitize_enabled(provider_id: str) -> bool:
    """Replicate the ``build_body`` gateway-family gate verbatim.

    Returns True when the real outbound request WOULD run gateway-blocked-term
    sanitization for this provider context, so the wire preview matches and is
    never more aggressive than reality.
    """
    _pid = provider_id.lower() if provider_id else ''
    return (_pid.startswith('sankuai')
            or (not _pid and 'sankuai' in getattr(_lib, 'LLM_BASE_URL', '')))


def apply_wire_sanitize(messages: list, *, conv_id: str = '',
                        provider_id: str = '',
                        user_id: int | None = None) -> list:
    """Return the OpenAI-form message array the model actually receives.

    Operates on a deep-ish copy (``_strip_non_api_fields`` returns shallow
    per-message copies) and never mutates the caller's list. This is the ONE
    implementation shared by the live snapshot and the ``/debug-messages``
    endpoint, so the cold and hot panels are byte-identical given the same
    ``provider_id``.

    Mirrors the model-agnostic tail of ``build_body`` in order:
      1. ``_strip_non_api_fields`` — drop frontend display metadata.
      2. ``_sanitize_messages`` — gateway-blocked-term replacement, GATED on
         ``_gateway_sanitize_enabled`` (verbatim build-body gate).
      3. ``_strip_empty_text_blocks`` — remove provider-invalid blank blocks.
      4. ``_fix_tool_call_wire_shape`` — normalize and occurrence-pair calls.
      5. ``_fix_orphaned_tool_calls`` — Anthropic orphan tool_use/result repair.
      6. ``_drop_empty_assistant_messages`` — pure-ghost assistant drop (strict
         providers HTTP 400 on empty assistant content).
      7. ``_merge_consecutive_same_role`` — consecutive user/assistant merge.
      8. ``_fix_empty_user_messages`` — empty-content placeholder.

    Args:
        messages: API-form messages (post system-context injection).
        conv_id: Compatibility routing identity; order is preserved.
        provider_id: Provider context for the gateway-sanitize gate. Empty →
            auto-detect from ``LLM_BASE_URL`` (matches the chat main loop).
        user_id: Compatibility owner identity; order is preserved.

    Returns:
        A new list of OpenAI-form messages.
    """
    work = [dict(message) for message in messages
            if isinstance(message, dict)]
    dropped_messages = len(messages) - len(work)
    if dropped_messages:
        logger.warning(
            '[wire_messages] Dropped %d malformed non-object message '
            'carrier(s) before snapshot sanitization', dropped_messages)
    clean = _strip_non_api_fields(
        work, carry_same_role_seam_hints=True)
    if _gateway_sanitize_enabled(provider_id):
        _sanitize_messages(clean)
    _strip_empty_text_blocks(clean)
    clean = _fix_tool_call_wire_shape(clean)
    clean = _fix_orphaned_tool_calls(clean)
    clean = _drop_empty_assistant_messages(clean)
    clean = _merge_consecutive_same_role(clean)
    _fix_empty_user_messages(clean)
    return clean


def build_wire_messages(raw_messages: list, config: dict, *,
                        user_id: int,
                        mode: str = 'snapshot',
                        task: dict | None = None,
                        conv_id: str = '',
                        provider_id: str = '',
                        return_manifest: bool = False):
    """Full wire-form pipeline for the debug panel (cold path).

    ``_transform_messages`` (DB → API form) → ``compose_task_context``
    (CLAUDE.md / static guidance / memory / date / swarm / preferences) →
    ``apply_wire_sanitize``.

    Args:
        raw_messages: Raw conversation messages (DB form).
        config: Task config (reads ``systemPrompt``, ``projectPath``,
            feature toggles via the same keys the live path uses).
        mode: ``'snapshot'`` (default) — side-effect-free reconstruction for
            the endpoint: a throwaway task + empty conv_id so inject neither
            consults live prefetch futures nor writes the live conv-keyed TTL
            caches; memory/date reflect a hypothetical first-round. ``'live'``
            — pass the real ``task`` so prefetch futures and the conv-keyed
            cache are honoured (used if a caller wants the exact live shape).
        task: Live task dict (``mode='live'`` only).
        conv_id: Conversation id. In ``'snapshot'`` mode this is NOT passed to
            inject (kept empty there for cache isolation) but IS forwarded to
            ``apply_wire_sanitize`` so the tool-result sort uses the right
            cache-prefix gate.
        provider_id: Provider context for the gateway-sanitize gate.

    Returns:
        The wire-form OpenAI message array. With ``return_manifest=True``,
        returns ``(messages, context_manifest)``.
    """
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    from lib.tasks_pkg.context_composer import compose_task_context

    msgs = _transform_messages(
        [dict(m) for m in raw_messages], config, user_id=user_id)

    if mode == 'live':
        _task = task if task is not None else {}
        _inject_cid = conv_id or (task.get('convId', '') if task else '')
    else:
        # Snapshot mode: throwaway task + empty conv_id → no live prefetch,
        # no conv-keyed cache writes. Side-effect chip state
        # (_appliedPreferences) lands on the throwaway and is discarded.
        _task = {'config': config}
        _inject_cid = ''

    project_path = config.get('projectPath') or ''
    project_enabled = bool(config.get('projectEnabled', bool(project_path)))
    memory_enabled = bool(config.get('memoryEnabled', False))
    search_enabled = bool(config.get('searchEnabled', True))

    try:
        compose_task_context(
            msgs,
            user_id=user_id,
            project_path=project_path,
            project_enabled=project_enabled,
            memory_enabled=memory_enabled,
            search_enabled=search_enabled,
            has_real_tools=True,
            conv_id=_inject_cid,
            task=_task,
            model=config.get('model', ''),
            system_prompt_mode=config.get('systemPromptMode', 'append'),
        )
    except Exception as e:
        logger.warning('[wire_messages] inject failed (mode=%s conv=%s): %s — '
                       'returning un-injected wire form', mode, (conv_id or '')[:8], e)

    wire = apply_wire_sanitize(
        msgs, conv_id=conv_id, provider_id=provider_id, user_id=user_id)
    if return_manifest:
        return wire, list(_task.get('_contextManifest') or [])
    return wire
