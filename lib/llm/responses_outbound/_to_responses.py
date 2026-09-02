"""lib/llm/responses_outbound/_to_responses.py — Chat Completions → Responses API.

Converts a canonical OpenAI Chat Completions request body into the OpenAI
**Responses API** shape (``POST /v1/responses``). Extracted from
``lib/oauth/codex.py:codex_translate_request`` (2026-07-31, epic
) and generalised from a Codex-only converter into the shared
boundary for EVERY Responses-speaking provider — the Codex-OAuth path is
now just one profile of it.

Conversion contract (the canonical body is the single IR — nothing else in
the app knows about Responses):

  * ``messages`` → ``input`` items: system → ``developer`` role; string
    content → ``input_text`` / ``output_text`` by role; ``image_url``
    blocks → ``input_image``; bare-tool_calls assistant messages →
    top-level ``function_call`` items; ``role='tool'`` →
    ``function_call_output`` keyed by ``call_id``.
  * ``tools[].function`` flattened to top-level fields (``strict: False``).
  * ``tool_choice`` function-dict flattened likewise; strings pass through.
  * Tool names truncated to 64 chars (the OpenAI function-name limit —
    applies to every Responses upstream, not just Codex).
  * ``store: False`` always — Tofu owns conversation state; server-side
    state (``previous_response_id``) is deliberately unused (DeepSeek
    doesn't support it; see memory ``responses_协议调研与_tofu_接缝图``).

Profile knobs (``RESPONSES_PROFILES``):

  * ``default`` — generic providers (DeepSeek …): keeps ``temperature`` /
    ``top_p``, maps ``max_tokens`` → ``max_output_tokens``, omits
    ``instructions`` / ``include``, reasoning effort without a summary
    channel (DeepSeek has reasoning but no summary).
  * ``codex``   — chatgpt.com/backend-api/codex: ``instructions: ''``,
    drops sampling params, ``include: ['reasoning.encrypted_content']``,
    ``reasoning.summary: 'auto'``.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'RESPONSES_PROFILES',
    'openai_body_to_responses',
    'responses_cache_affinity_key',
]

#: Per-upstream dialect knobs. ``instructions=None`` → omit the field.
RESPONSES_PROFILES: dict = {
    'default': {
        'instructions': None,
        'drop_params': (),
        'map_max_tokens': True,
        'reasoning_summary': None,
        'include': (),
        'parallel_tool_calls': True,
    },
    'codex': {
        'instructions': '',
        'drop_params': ('temperature', 'top_p', 'max_tokens'),
        'map_max_tokens': False,
        'reasoning_summary': 'auto',
        'include': ('reasoning.encrypted_content',),
        'parallel_tool_calls': True,
    },
}

#: OpenAI function-name hard limit (all Responses upstreams share it).
_MAX_TOOL_NAME = 64
_TOOL_SEARCH_MIN_FUNCTIONS = 16
_TOOL_SEARCH_MIN_DEFERRED = 8
_TOOL_SEARCH_NAMESPACE_SIZE = 10

# Opaque output items safe/necessary to replay when ``store=false``.  The
# response adapters capture this same set.  Unknown items are observable but
# are not blindly replayed onto an upstream protocol.
_REPLAY_RESPONSE_ITEM_TYPES = frozenset({
    'reasoning', 'program', 'program_output',
    'tool_search_call', 'tool_search_output',
    'multi_agent_call', 'multi_agent_call_output', 'agent_message',
    'shell_call', 'shell_call_output', 'code_interpreter_call',
    'web_search_call', 'file_search_call', 'computer_call',
})


def _is_gpt_56(model: str) -> bool:
    """Return whether *model* is in the GPT-5.6 family.

    The cache-breakpoint and server-compaction request fields are intentionally
    scoped to this family.  Generic Responses-compatible providers frequently
    reject unknown OpenAI fields instead of ignoring them.
    """
    from lib.model_info._openai_gpt56 import is_official_gpt56_model
    return is_official_gpt56_model(model)


def responses_cache_affinity_key(body: dict) -> str:
    """Build the stable, non-identifying cache/session key for a conversation.

    The ChatGPT Codex transport uses this same lifetime for both the Responses
    ``prompt_cache_key`` and its session-affinity headers.  Keeping the digest
    in one helper prevents those two routing hints from silently drifting.
    """
    namespace = body.get('_conv_id') or body.get('_task_id') or ''
    if not namespace:
        return ''
    digest = hashlib.sha256(
        f'tofu-responses-cache:{namespace}'.encode('utf-8')
    ).digest()[:16]
    # Codex CLI sends one UUID-shaped value verbatim in prompt_cache_key,
    # session-id, thread-id, and x-client-request-id.  Match that contract
    # exactly while deriving it deterministically from Tofu's private id.
    return str(uuid.UUID(bytes=digest, version=5))


def _add_stable_prefix_breakpoint(items: list[dict]) -> bool:
    """Mark the stable developer/task prefix as an explicit cache.

    GPT-5.6's implicit breakpoint follows the newest user/tool item, which is
    dynamic in an agent loop.  A second, explicit breakpoint on the stable
    instruction prefix provides a reliable cache floor when the long dynamic
    prefix is evicted or cannot be reused.
    """
    candidate = None
    saw_developer = False
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'message':
            if saw_developer:
                break
            continue
        if item.get('role') != 'developer':
            if saw_developer:
                break
            continue
        saw_developer = True
        for part in item.get('content') or ():
            if (isinstance(part, dict)
                    and part.get('type') == 'input_text'
                    and part.get('text')):
                candidate = part
    # Stateless/compat callers may not provide a system message. Their first
    # user task is still stable across an agent loop and is the safest fallback
    # cache floor; never mark a later dynamic tool/user item.
    if candidate is None:
        for item in items:
            if (not isinstance(item, dict) or item.get('type') != 'message'
                    or item.get('role') != 'user'):
                continue
            for part in item.get('content') or ():
                if (isinstance(part, dict)
                        and part.get('type') == 'input_text'
                        and part.get('text')):
                    candidate = part
            break
    if candidate is None:
        return False
    candidate['prompt_cache_breakpoint'] = {'mode': 'explicit'}
    return True


def _response_items(msg: dict, *, allow_compaction: bool) -> list[dict]:
    """Return replay-safe opaque Responses items stored on a chat message."""
    allowed = set(_REPLAY_RESPONSE_ITEM_TYPES)
    if allow_compaction:
        allowed.add('compaction')
    out = []
    for item in msg.get('_responses_items') or ():
        if isinstance(item, dict) and item.get('type') in allowed:
            # Shallow-copy the envelope so adding request-local metadata later
            # can never mutate the persisted conversation row.
            out.append(dict(item))
    return out


def _responses_text_format(response_format) -> dict | None:
    """Map Chat-Completions ``response_format`` to Responses ``text.format``."""
    if not isinstance(response_format, dict):
        return None
    kind = str(response_format.get('type') or '').strip()
    if kind == 'json_schema':
        schema = response_format.get('json_schema')
        if not isinstance(schema, dict):
            return None
        converted = {'type': 'json_schema'}
        for key in ('name', 'schema', 'strict', 'description'):
            if key in schema:
                converted[key] = schema[key]
        return converted
    if kind in ('json_object', 'text'):
        return {'type': kind}
    return None


def _safe_namespace(value: str) -> str:
    """Return a stable Responses namespace identifier."""
    normalized = re.sub(r'[^a-z0-9_-]+', '_', str(value or '').lower())
    normalized = normalized.strip('_') or 'general'
    return normalized[:48]


def _tool_search_surface(tools: list[dict], *, pinned_names: set[str],
                         namespace_by_name: dict[str, str]) -> list[dict]:
    """Defer only the residual tool catalog behind hosted Tool Search.

    ``pinned_names`` is authoritative: every matching function stays at the
    top level with no ``defer_loading`` marker.  This is the protocol-boundary
    enforcement of the frontend/caller selection contract.
    """
    functions = [tool for tool in tools
                 if isinstance(tool, dict) and tool.get('type') == 'function']
    deferred = [tool for tool in functions
                if str(tool.get('name') or '') not in pinned_names]
    if (len(functions) < _TOOL_SEARCH_MIN_FUNCTIONS
            or len(deferred) < _TOOL_SEARCH_MIN_DEFERRED):
        return tools

    direct: list[dict] = []
    groups: dict[str, list[dict]] = {}
    group_order: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get('type') != 'function':
            direct.append(tool)
            continue
        name = str(tool.get('name') or '')
        if name in pinned_names:
            # Defensive copy also guarantees a caller-supplied stale marker
            # can never accidentally defer a frontend-selected tool.
            selected = dict(tool)
            selected.pop('defer_loading', None)
            direct.append(selected)
            continue
        namespace = _safe_namespace(namespace_by_name.get(name) or 'general')
        if namespace not in groups:
            groups[namespace] = []
            group_order.append(namespace)
        deferred_tool = dict(tool)
        deferred_tool['defer_loading'] = True
        groups[namespace].append(deferred_tool)

    namespaces: list[dict] = []
    used_names: set[str] = set()
    for base in group_order:
        members = groups[base]
        for offset in range(0, len(members), _TOOL_SEARCH_NAMESPACE_SIZE):
            chunk = members[offset:offset + _TOOL_SEARCH_NAMESPACE_SIZE]
            suffix = offset // _TOOL_SEARCH_NAMESPACE_SIZE + 1
            name = base if len(members) <= _TOOL_SEARCH_NAMESPACE_SIZE \
                else f'{base}_{suffix}'
            while name in used_names:
                suffix += 1
                name = f'{base}_{suffix}'
            used_names.add(name)
            namespaces.append({
                'type': 'namespace',
                'name': name,
                'description': (
                    f'{base.replace("_", " ")} tools available on demand.'),
                'tools': chunk,
            })

    direct_names = {
        str(tool.get('name') or '') for tool in direct
        if isinstance(tool, dict) and tool.get('type') == 'function'}
    missing = pinned_names.intersection(
        str(tool.get('name') or '') for tool in functions) - direct_names
    if missing:
        # Fail open to the original full surface.  This branch should be
        # unreachable, but correctness beats token savings if policy drifts.
        logger.error('[Responses] Tool Search pin invariant failed for %s; '
                     'sending the full tool catalog', sorted(missing))
        return tools
    logger.info('[Responses] Tool Search: %d direct, %d deferred across %d '
                'namespace(s)', len(direct_names), len(deferred),
                len(namespaces))
    return [{'type': 'tool_search'}] + direct + namespaces


def _programmatic_eligible_names() -> set[str]:
    """Return native functions explicitly reviewed for GPT-5.6 PTC."""
    try:
        from lib.tools.programmatic import eligible_programmatic_tool_names
        return eligible_programmatic_tool_names()
    except Exception as exc:
        logger.warning('[Responses] PTC eligibility lookup failed: %s', exc)
        return set()


def _enable_programmatic_tools(tools: list[dict], *,
                               eligible: set[str] | None = None) -> list[dict]:
    """Opt trusted read-only functions into direct + programmatic invocation."""
    if eligible is None:
        eligible = _programmatic_eligible_names()
    from lib.tools.programmatic import programmatic_output_schema

    converted: list[dict] = []
    enabled = 0
    for tool in tools:
        copied = dict(tool)
        if copied.get('type') == 'function' and copied.get('name') in eligible:
            copied['allowed_callers'] = ['direct', 'programmatic']
            # Native handlers return canonical text. The replay boundary wraps
            # both direct and program-issued results in this exact envelope.
            copied['output_schema'] = programmatic_output_schema()
            enabled += 1
        converted.append(copied)
    if enabled:
        converted.append({'type': 'programmatic_tool_calling'})
    return converted


def _inject_programmatic_guidance(items: list[dict], *, stage: str = '') -> None:
    """Add the bounded read-only routing boundary recommended by OpenAI Docs."""
    from lib.tools.programmatic import (
        PROGRAMMATIC_MAX_CALLS,
        PROGRAMMATIC_MAX_CONCURRENT_CALLS,
        PROGRAMMATIC_MAX_CONTINUATIONS,
        PROGRAMMATIC_MAX_OUTPUT_BYTES,
    )

    bounded_stage = str(stage or '').strip() or (
        'process several eligible read-only tool results into a compact, '
        'evidence-backed comparison or validation')
    guidance = (
        'Use Programmatic Tool Calling only for this bounded read-only stage: '
        f'{bounded_stage}. '
        'The program may filter, join, rank, deduplicate, aggregate, or validate several '
        'eligible tool results. Every eligible tool returns exactly '
        '{content:string,truncated:boolean}; stop and report a structured '
        'failure when truncated is true and the missing bytes are required. '
        f'Use at most {PROGRAMMATIC_MAX_CALLS} child calls and '
        f'{PROGRAMMATIC_MAX_CONCURRENT_CALLS} concurrent child calls, with '
        f'{PROGRAMMATIC_MAX_OUTPUT_BYTES} UTF-8 bytes of child output per '
        f'program, with at most {PROGRAMMATIC_MAX_CONTINUATIONS} continuation '
        'responses. Emit one compact JSON result with status, findings, and '
        'evidence fields. Keep the reduced result and required evidence. '
        'Use direct tool calls for semantic judgment, writes, approvals, and '
        'final artifact validation. Do not retry a failed call more than once.'
    )
    insert_at = 0
    while (insert_at < len(items)
           and items[insert_at].get('type') == 'message'
           and items[insert_at].get('role') == 'developer'):
        insert_at += 1
    items.insert(insert_at, {
        'type': 'message', 'role': 'developer',
        'content': [{'type': 'input_text', 'text': guidance}],
    })


def _truncate_name(name: str, reverse: dict) -> str:
    """Clamp a tool name to the 64-char limit, recording the mapping so
    the response side can restore the model's echo (first-original wins,
    mirroring CLIProxyAPI recordRename semantics)."""
    if len(name) <= _MAX_TOOL_NAME:
        return name
    truncated = name[:_MAX_TOOL_NAME]
    reverse.setdefault(truncated, name)
    return truncated


def openai_body_to_responses(body: dict, *, profile: str = 'default',
                             stream: bool = False) -> tuple:
    """Translate a Chat Completions request body to Responses API format.

    Args:
        body: the canonical OpenAI-shaped body (mutated NOT — a new dict
            is built from an allowlist, so internal keys like ``_task_id``
            never leak onto the wire).
        profile: key of :data:`RESPONSES_PROFILES`.
        stream: value for the ``stream`` field.

    Returns:
        ``(responses_body, tool_name_reverse)`` — the Responses API request
        body, plus the per-request reverse map ``{truncated: original}``
        for tool names shortened to the 64-char function-name limit. The
        map must ride the response-side translator so echoed names are
        restored before tool dispatch (mirrors ``apply_claude_cloak``).
    """
    prof = RESPONSES_PROFILES.get(profile)
    if prof is None:
        logger.warning('[Responses] unknown profile %r — falling back to '
                       "'default'", profile)
        prof = RESPONSES_PROFILES['default']

    model = body.get('model', '')
    is_gpt_56 = _is_gpt_56(model)
    feature_profile = str(
        body.get('_responses_feature_profile') or 'compatible').lower()
    public_openai_features = (
        is_gpt_56 and profile != 'codex' and feature_profile == 'openai')
    stateful_gpt56 = is_gpt_56 and (
        public_openai_features or profile == 'codex')
    breakpoint_mode = str(
        body.get('_gpt56_breakpoint_mode') or 'implicit').lower()
    programmatic_mode = str(
        body.get('_programmatic_tool_calling') or 'off').lower()
    tool_search_mode = str(
        body.get('_tool_search_mode') or 'off').lower()
    from lib.tools.programmatic import ACTIVE_PROGRAMMATIC_MODES
    programmatic_enabled = (
        public_openai_features
        and programmatic_mode in ACTIVE_PROGRAMMATIC_MODES)
    # The wire boundary resolves native_openai vs local per request; a local
    # resolution must never leak the hosted-only PTC fields onto a generic
    # Responses upstream.  An absent key preserves the legacy gate verbatim.
    _resolved_ptc = str(
        body.get('_resolved_programmatic_backend') or '').lower()
    if _resolved_ptc and _resolved_ptc != 'native_openai':
        programmatic_enabled = False
    multi_agent_enabled = (
        public_openai_features
        and str(body.get('_multi_agent_mode') or '').lower() == 'read_only')
    # Like PTC, provider-native multi-agent is enabled by the final resolved
    # backend, never by model-name inference inside this converter.  An absent
    # key preserves direct-call compatibility with older callers.
    _resolved_multi_agent = str(
        body.get('_resolved_multi_agent_backend') or '').lower()
    if (_resolved_multi_agent
            and _resolved_multi_agent != 'native_openai'):
        multi_agent_enabled = False
    resolved_tool_search = str(
        body.get('_resolved_tool_search_backend') or '').lower()
    tool_search_enabled = (
        resolved_tool_search == 'native_openai'
        if resolved_tool_search
        else public_openai_features and tool_search_mode == 'auto')
    programmatic_names = (_programmatic_eligible_names()
                          if programmatic_enabled else set())
    out: dict = {
        'model': model,
        'store': False,
        'stream': stream,
    }
    if prof['instructions'] is not None:
        out['instructions'] = prof['instructions']
    if prof['parallel_tool_calls']:
        out['parallel_tool_calls'] = True

    # Sampling params — kept for generic providers, dropped for Codex.
    drop = set(prof['drop_params'])
    for k in ('temperature', 'top_p'):
        if k in body and k not in drop:
            out[k] = body[k]
    if 'max_tokens' in body and 'max_tokens' not in drop:
        if prof['map_max_tokens']:
            out['max_output_tokens'] = body['max_tokens']
        else:
            out['max_tokens'] = body['max_tokens']

    # Responses moved Chat Completions' ``response_format`` under
    # ``text.format``.  Preserve any caller-supplied text options, then apply
    # the canonical structured-output mapping and GPT-5.6 verbosity.
    text_config = (dict(body.get('text'))
                   if isinstance(body.get('text'), dict) else {})
    text_format = _responses_text_format(body.get('response_format'))
    if text_format is not None:
        text_config['format'] = text_format
    verbosity = str(body.get('_text_verbosity') or '').lower()
    if (public_openai_features
            and verbosity in ('low', 'medium', 'high')):
        text_config['verbosity'] = verbosity
    if text_config:
        out['text'] = text_config

    # Reasoning effort. ``summary`` only where the upstream has that
    # channel (Codex); DeepSeek has reasoning but no summary. Codex is
    # always a reasoning model — its profile defaults effort to 'medium'.
    effort = body.get('reasoning_effort')
    if is_gpt_56 and effort:
        from lib.model_info._openai_gpt56 import (
            normalize_gpt56_reasoning_effort)
        effort = normalize_gpt56_reasoning_effort(effort)
    if profile == 'codex' and not effort:
        effort = 'medium'
    if profile == 'codex':
        # ChatGPT's Codex Responses endpoint follows the CLIProxyAPI
        # subscription registry, whose current top rung is xhigh before 5.6
        # and max on 5.6+.  It also uses ``none`` rather than the legacy
        # ``minimal`` spelling. Keep generic GPT/API providers untouched.
        if effort in ('off', 'minimal'):
            effort = 'none'
        elif effort in ('max', 'ultra'):
            effort = 'max' if is_gpt_56 else 'xhigh'
    if effort or prof['reasoning_summary'] is not None or stateful_gpt56:
        reasoning: dict = {}
        if effort:
            reasoning['effort'] = effort
        if prof['reasoning_summary'] is not None:
            reasoning['summary'] = prof['reasoning_summary']
        if stateful_gpt56:
            # We resend encrypted reasoning items in stateless mode below, so
            # GPT-5.6 can retain reasoning across the full agent loop.
            reasoning['context'] = 'all_turns'
            if (public_openai_features
                    and str(body.get('_reasoning_mode') or '').lower()
                    == 'pro'):
                reasoning['mode'] = 'pro'
        out['reasoning'] = reasoning

    if public_openai_features:
        safety_identifier = str(body.get('_safety_identifier') or '').strip()
        if safety_identifier:
            out['safety_identifier'] = safety_identifier[:64]
        if multi_agent_enabled:
            try:
                max_subagents = int(
                    body.get('_multi_agent_max_concurrent_agents')
                    or body.get('_multi_agent_max_concurrent_subagents') or 3)
            except (TypeError, ValueError) as exc:
                logger.debug('[ResponsesOut] invalid max-subagents value: %s', exc)
                max_subagents = 3
            out['multi_agent'] = {
                'enabled': True,
                'max_concurrent_subagents': max(1, min(max_subagents, 8)),
            }

    includes = list(prof['include'])
    if stateful_gpt56 and 'reasoning.encrypted_content' not in includes:
        includes.append('reasoning.encrypted_content')
    if includes:
        out['include'] = includes

    reverse: dict = {}  # truncated tool name → original (response side restores)

    out['input'] = _messages_to_input(
        body.get('messages') or [], reverse,
        replay_response_items=stateful_gpt56,
        allow_compaction=public_openai_features,
        programmatic_tool_names=programmatic_names,
        default_image_detail=(
            'original' if (public_openai_features
                           and str(body.get('_image_detail') or '').lower()
                           == 'original') else ''),
    )

    if out.get('multi_agent'):
        # This beta path is intentionally analysis-only.  Existing dispatch
        # approval/write gates remain authoritative for root tool calls.
        insert_at = 0
        while (insert_at < len(out['input'])
               and out['input'][insert_at].get('type') == 'message'
               and out['input'][insert_at].get('role') == 'developer'):
            insert_at += 1
        out['input'].insert(insert_at, {
            'type': 'message', 'role': 'developer',
            'content': [{
                'type': 'input_text',
                'text': (
                    'Native subagents are read-only analysts. Delegate only '
                    + (str(body.get('_multi_agent_stage') or '').strip()
                       or 'independent research, inspection, comparison, or verification')
                    + '. '
                    'Subagents must not mutate files, external '
                    'systems, user state, schedules, browser state, or shared '
                    'project state. The root agent owns every action.'),
            }],
        })

    if stateful_gpt56:
        cache_key = responses_cache_affinity_key(body)
        if cache_key:
            out['prompt_cache_key'] = cache_key

        # Local structured compaction remains the primary path.  Server-side
        # compaction is a stateless safety net at the same user-approved
        # economic working-set ceiling on the public Responses API; its opaque
        # output is captured and replayed by the response translators. Codex's
        # subscription backend owns compaction itself and does not expose this
        # public request field, so the codex profile keeps local compaction only.
        try:
            compact_threshold = int(body.get('_working_set_tokens') or 0)
        except (TypeError, ValueError) as e:
            logger.debug('[Responses] invalid working-set threshold %r: %s',
                         body.get('_working_set_tokens'), e)
            compact_threshold = 0
        if (compact_threshold > 0 and public_openai_features
                and not out.get('multi_agent')):
            out['context_management'] = [{
                'type': 'compaction',
                'compact_threshold': compact_threshold,
            }]

    tools = body.get('tools')
    if tools:
        out['tools'] = _convert_tools(tools, reverse)
        if programmatic_enabled:
            out['tools'] = _enable_programmatic_tools(
                out['tools'], eligible=programmatic_names)
            if any(tool.get('type') == 'programmatic_tool_calling'
                   for tool in out['tools']):
                _inject_programmatic_guidance(
                    out['input'], stage=body.get('_programmatic_stage') or '')

        if tool_search_enabled:
            raw_pins = body.get('_frontend_selected_tool_names') or ()
            pinned_names = {
                _truncate_name(str(name), reverse)
                for name in raw_pins if str(name or '')
            }
            choice = body.get('tool_choice')
            if isinstance(choice, dict) and choice.get('type') == 'function':
                forced = str((choice.get('function') or {}).get('name') or '')
                if forced:
                    pinned_names.add(_truncate_name(forced, reverse))
            raw_namespaces = body.get('_tool_namespace_by_name') or {}
            namespace_by_name = {}
            if isinstance(raw_namespaces, dict):
                namespace_by_name = {
                    _truncate_name(str(name), reverse): str(namespace)
                    for name, namespace in raw_namespaces.items()
                    if str(name or '')
                }
            out['tools'] = _tool_search_surface(
                out['tools'], pinned_names=pinned_names,
                namespace_by_name=namespace_by_name)

    # Mark after optional PTC guidance is injected so that stable developer
    # instruction is part of the explicit floor instead of landing in the
    # dynamic suffix. ``chatgpt.com/backend-api/codex`` is not the public
    # Responses API and rejects this public per-content field on some models.
    if public_openai_features:
        marked = _add_stable_prefix_breakpoint(out['input'])
        if breakpoint_mode == 'explicit' and marked:
            # Disable GPT-5.6's automatic latest-message breakpoint. Only the
            # stable marker above remains eligible for cache writes.
            out['prompt_cache_options'] = {'mode': 'explicit'}
        elif breakpoint_mode == 'explicit':
            logger.warning('[Responses] explicit-only cache requested but no '
                           'stable text prefix was available; retaining '
                           'implicit mode for this request')
    choice = body.get('tool_choice')
    if choice:
        out['tool_choice'] = _convert_tool_choice(choice, reverse)

    return out, reverse


def _messages_to_input(messages: list, reverse: dict, *,
                       replay_response_items: bool = False,
                       allow_compaction: bool = False,
                       programmatic_tool_names: set[str] | None = None,
                       default_image_detail: str = '') -> list:
    """OpenAI messages[] → Responses input[] items."""
    items: list = []
    ptc_names = programmatic_tool_names or set()

    # role='tool' carries only call_id. Recover its function name so direct and
    # programmatic invocations of an opted-in tool share one output contract.
    call_names: dict[str, str] = {}
    for msg in messages:
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        for tc in msg.get('tool_calls') or ():
            if not isinstance(tc, dict):
                continue
            call_id = str(tc.get('id') or '')
            name = str((tc.get('function') or {}).get('name') or '')
            if call_id and name:
                call_names[call_id] = name

    program_output_bytes: dict[str, int] = {}

    # A compaction item carries the state of all earlier dynamic input.  Keep
    # leading system/developer instructions current, but do not resend the old
    # user/tool transcript before the newest compaction carrier.
    compact_at = None
    if replay_response_items and allow_compaction:
        for index, msg in enumerate(messages):
            if any(item.get('type') == 'compaction'
                   for item in _response_items(msg, allow_compaction=True)):
                compact_at = index

    for index, msg in enumerate(messages):
        role = msg.get('role', '')
        content = msg.get('content')

        if compact_at is not None and index < compact_at and role != 'system':
            continue

        if replay_response_items and role == 'assistant':
            items.extend(_response_items(
                msg, allow_compaction=allow_compaction))

        if role == 'tool':
            # Tool results join their call by call_id — the stable identity
            # (the stream-side item id is only an in-stream index).
            output = (content if isinstance(content, str)
                      else json.dumps(content if content is not None else '',
                                      ensure_ascii=False))
            caller = msg.get('caller')
            call_id = str(msg.get('tool_call_id') or '')
            tool_name = call_names.get(call_id, '')
            is_program = (isinstance(caller, dict)
                          and caller.get('type') == 'program')
            if is_program or tool_name in ptc_names:
                from lib.tools.programmatic import (
                    PROGRAMMATIC_MAX_OUTPUT_BYTES,
                    encode_programmatic_output,
                )
                max_bytes = None
                caller_id = ''
                if is_program:
                    caller_id = str(caller.get('caller_id') or '')
                    used = program_output_bytes.get(caller_id, 0)
                    max_bytes = max(0, PROGRAMMATIC_MAX_OUTPUT_BYTES - used)
                output, consumed, _truncated = encode_programmatic_output(
                    output, max_bytes=max_bytes)
                if is_program:
                    program_output_bytes[caller_id] = (
                        program_output_bytes.get(caller_id, 0) + consumed)
            tool_output = {
                'type': 'function_call_output',
                'call_id': call_id,
                'output': output,
            }
            if (isinstance(caller, dict)
                    and caller.get('type') != 'multi_agent'):
                tool_output['caller'] = dict(caller)
            items.append(tool_output)
            continue

        api_role = 'developer' if role == 'system' else role
        content_parts: list = []
        if isinstance(content, str) and content:
            part_type = 'output_text' if role == 'assistant' else 'input_text'
            content_parts.append({'type': part_type, 'text': content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get('type')
                if btype == 'text':
                    part_type = ('output_text' if role == 'assistant'
                                 else 'input_text')
                    content_parts.append(
                        {'type': part_type, 'text': block.get('text', '')})
                elif btype == 'image_url' and role == 'user':
                    image = block.get('image_url') or {}
                    if isinstance(image, str):
                        url = image
                        detail = ''
                    else:
                        url = image.get('url', '')
                        detail = str(image.get('detail') or '').lower()
                    if url:
                        input_image = {
                            'type': 'input_image', 'image_url': url}
                        if detail in ('low', 'high', 'auto', 'original'):
                            input_image['detail'] = detail
                        elif default_image_detail:
                            input_image['detail'] = default_image_detail
                        content_parts.append(input_image)

        # An assistant message with no text payload emits no message item —
        # its tool calls below stand alone as function_call items.
        if role != 'assistant' or content_parts:
            items.append({'type': 'message', 'role': api_role,
                          'content': content_parts})

        # Assistant tool_calls → top-level function_call items (whether or
        # not the message carried text).
        for tc in msg.get('tool_calls') or []:
            if tc.get('type') != 'function':
                continue
            func = tc.get('function') or {}
            function_call = {
                'type': 'function_call',
                'call_id': tc.get('id', ''),
                'name': _truncate_name(func.get('name', ''), reverse),
                'arguments': func.get('arguments', '{}'),
            }
            caller = tc.get('caller')
            if isinstance(caller, dict):
                if (caller.get('type') == 'multi_agent'
                        and caller.get('agent_name')):
                    function_call['agent'] = {
                        'agent_name': str(caller['agent_name'])}
                else:
                    function_call['caller'] = dict(caller)
            items.append(function_call)
    return items


def _convert_tools(tools: list, reverse: dict) -> list:
    """Chat-Completions tools[] → Responses tools[] (flattened function).

    Non-function tools pass through untouched (server-side built-ins like
    ``web_search`` have no chat-completions wrapper). Function fields are
    carried only when the source specifies them — the converter mirrors,
    it does not invent.
    """
    converted: list = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get('type') != 'function':
            converted.append(tool)
            continue
        func = tool.get('function') or {}
        t: dict = {'type': 'function',
                   'name': _truncate_name(func.get('name', ''), reverse)}
        if func.get('description'):
            t['description'] = func['description']
        if func.get('parameters'):
            t['parameters'] = func['parameters']
        if func.get('strict') is not None:
            t['strict'] = func['strict']
        converted.append(t)
    return converted


def _convert_tool_choice(choice, reverse: dict):
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict) and choice.get('type') == 'function':
        return {'type': 'function',
                'name': _truncate_name(
                    (choice.get('function') or {}).get('name', ''), reverse)}
    return choice
