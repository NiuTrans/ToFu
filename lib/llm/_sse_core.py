# HOT_PATH
"""Transport-agnostic core for streaming chat completions.

Both ``lib/llm/stream.py`` (sync, ``requests``) and ``lib/llm/astream.py``
(async, ``httpx``) used to carry a ~480-line copy of the *identical* SSE
chunk-parsing loop: error classification, MiniMax ``<think>`` demux,
tool-call accumulation, premature-close / empty-stop anomaly diagnostics,
and ``usage`` metadata injection. Every fix had to land twice and the two
copies drifted.

This module holds that logic exactly once. The two transport shells keep
only what genuinely differs:

  - opening the stream + iterating raw bytes (``requests`` vs ``httpx``);
  - the retry/backoff loop's sleep call (blocking vs ``await``);
  - mapping ``httpx`` transport exceptions to ``RetryableAPIError``.

Public surface
--------------
  - ``prepare_request(body, *, attempt, log_prefix, api_key, base_url,
    extra_headers) -> RequestPlan`` — the identical pre-flight (cache
    breakpoints, extended-TTL header, wire-protocol translation, header build,
    URL resolution, RawSSEDumper start).
  - ``classify_status_error(status_code, err_text, *, body, log_prefix,
    raw_dumper)`` — shared non-200 handling (delegates to
    ``_classify_http_error``); the caller reads the error body in its own
    transport-native way and passes the text in.
  - ``SSEAccumulator`` — frame raw bytes once with ``SSEFramer``, submit each
    complete ``SSEEvent`` through ``feed_event(event)``, then call
    ``finalize(...)`` for a tuple-compatible
    :class:`ProviderStreamResult`. The historical ``feed_line`` adapter is
    retained only for plugin/test migration. Payload submission raises the
    same exceptions the inline loop did
    (``ModelLimitError`` / ``RateLimitError`` / ``PromptTooLongError`` /
    ``RetryableAPIError`` / ``Exception('SSE error: …')``), so the
    transport shell's retry wrapper handles them unchanged.

The accumulator records normalized progress into one ``StreamProgress`` and
closes one typed ``ProviderStreamResult``. Historical ``usage`` anomaly fields
are compatibility projections of that result/evidence; downstream callers do
not reconstruct completion from an open-ended flag bag.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Optional

import lib as _lib
from lib.llm._sse_framer import SSEEvent
from lib.llm._transport import StreamProgress, chat_url, headers
from lib.llm.cache import add_cache_breakpoints
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.stream_result import (
    ProviderStreamResult,
    classify_provider_stream_state,
)
from lib.cost import canonicalize_usage_cache_keys, normalize_usage
from lib.llm_errors import (
    ModelLimitError,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    _ERR_BODY_LIMIT,
    _GATEWAY_THROTTLE_STATUS,
    _classify_http_error,
    _is_prompt_too_long,
    repair_mojibake,
)
from lib.log import get_logger
from lib.model_info import (
    _learn_model_limit,
    _parse_token_limit_from_error,
    is_claude,
    is_minimax,
)

logger = get_logger(__name__)

_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')
_MAX_CONSECUTIVE_PARSE_ERRORS = 10

# ── Cache byte-probe (diagnostic, default OFF, zero production impact) ──
# When TOFU_CACHE_BYTE_PROBE is set to a conv-id prefix, prepare_request dumps
# the FINAL post-translation body (the exact messages+system+tools bytes handed
# to the transport, AFTER add_cache_breakpoints AND openai_body_to_anthropic)
# for the matching conversation, on each round, to
# ``.tofu_cache_probe/<conv>/round_NNNN_<trace>.json``. A standalone analyzer
# (debug/cache_byte_probe_diff.py) then diffs two consecutive rounds at the RAW
# byte level — deliberately NOT through canonical_messages — to settle whether
# a "PROVEN server-side" cache miss is actually a client-caused prefix mutation
# the canonical fingerprint erased. Unset ⇒ the whole block is skipped.
_CACHE_PROBE_ROUND: dict = {}


def _cache_probe_stable_ttls(body):
    """Collect every ``cache_control`` marker's ttl + a coarse location, so the
    analyzer can tell a stable-block ttl flip (1h↔absent) from a body change.

    Returns a list of ``{loc, ttl}`` in wire order. ``ttl`` is ``''`` for a
    bare ``{'type':'ephemeral'}`` marker (5-minute default). Best-effort.
    """
    out = []

    def _scan(container, loc):
        if isinstance(container, dict):
            cc = container.get('cache_control')
            if isinstance(cc, dict):
                out.append({'loc': loc, 'ttl': cc.get('ttl', '')})
            content = container.get('content')
            if isinstance(content, list):
                for j, blk in enumerate(content):
                    _scan(blk, f'{loc}.content[{j}]')
        # else: str content carries no marker

    # Anthropic path: system + tools live at the top level; messages below.
    sysblk = body.get('system')
    if isinstance(sysblk, list):
        for i, blk in enumerate(sysblk):
            _scan(blk, f'system[{i}]')
    tools = body.get('tools')
    if isinstance(tools, list):
        for i, t in enumerate(tools):
            _scan(t, f'tools[{i}]')
    for i, m in enumerate(body.get('messages') or []):
        _scan(m, f'messages[{i}]')
    return out


def _maybe_dump_cache_probe(body, task_id, log_prefix='', routing=None):
    """Dump the final post-translation body for a targeted conv (diagnostic).

    Gated on ``TOFU_CACHE_BYTE_PROBE`` (a conv-id prefix). Resolves the conv id
    from ``task_id`` via the chat runtime, and only dumps when it matches the
    target. Best-effort: any failure is logged at debug and never blocks a
    request. This does NOT canonicalize — it writes the literal body dict so
    the analyzer sees the exact wire bytes.

    ``routing`` (optional) carries the per-request routing fingerprint — key
    discriminator, endpoint, final ``anthropic-beta`` header — so a raw-byte
    round-over-round diff can distinguish a BODY-byte flip from a cache-NAMESPACE
    change (same bytes routed to a different key/endpoint → different gateway
    cache pool → floor miss on an otherwise byte-identical prefix). This is the
    dimension the mrne3bqe R4 clean-round miss (byte-identical, no retry) needs.
    """
    import os
    target = os.environ.get('TOFU_CACHE_BYTE_PROBE', '').strip()
    if not target:
        return
    try:
        conv_id = ''
        if task_id:
            try:
                from lib.tasks_pkg.manager.runtime import chat_task_runtime
                _t = chat_task_runtime.get(task_id)
                if _t:
                    conv_id = _t.get('convId') or ''
            except Exception as _re:
                logger.debug('%s cache-probe conv resolve failed: %s', log_prefix, _re)
        # Match on conv-id prefix; if the conv is unknown, fall back to task id
        # so a probe can still target a task that isn't in the conv index.
        key = conv_id or task_id
        if not key or not key.startswith(target):
            return

        import json as _json
        import time as _time
        from lib.agent_artifacts import ARTIFACT_PREFIX
        base = os.path.join(os.getcwd(), f'{ARTIFACT_PREFIX}_cache_probe', key)
        os.makedirs(base, exist_ok=True)
        rnd = _CACHE_PROBE_ROUND.get(key, 0)
        _CACHE_PROBE_ROUND[key] = rnd + 1
        # Dump the exact system/messages/tools that go on the wire. Use the
        # SAME serialization the transport uses (ensure_ascii=False) so byte
        # lengths match what is actually sent.
        snapshot = {
            'round': rnd,
            'ts': _time.time(),
            'conv_id': conv_id,
            'task_id': task_id,
            'model': body.get('model', ''),
            # ── Routing fingerprint (cache-NAMESPACE dimension) ──
            # Same body bytes routed to a different key/endpoint land in a
            # different gateway cache pool → floor miss on a byte-identical
            # prefix (the mrne3bqe R4 clean-round hypothesis). The API key is
            # NEVER dumped raw — only a short salted hash as a stable "which
            # key" discriminator (CLAUDE.md §2.6: never log secrets).
            'routing': routing or {},
            # Stable-block cache_control ttl values, in wire order. A 1h↔absent
            # flip here shifts the Anthropic cache key even when body bytes and
            # marker COUNT are unchanged (the detector's historical blind spot).
            'stable_ttls': _cache_probe_stable_ttls(body),
            'system': body.get('system'),
            'tools': body.get('tools'),
            'messages': body.get('messages') or body.get('input') or [],
            'prompt_cache_key': body.get('prompt_cache_key'),
            'context_management': body.get('context_management'),
        }
        path = os.path.join(base, f'round_{rnd:04d}.json')
        with open(path, 'w', encoding='utf-8') as fh:
            _json.dump(snapshot, fh, ensure_ascii=False)
        logger.warning('%s [CacheProbe] dumped round=%d conv=%s → %s',
                       log_prefix, rnd, key[:12], path)
    except Exception as e:
        logger.debug('%s cache byte-probe dump failed: %s', log_prefix, e)


@dataclass
class RequestPlan:
    """Everything a transport shell needs to open the stream."""
    url: str
    hdrs: dict
    body: dict
    trace_id: str
    raw_dumper: RawSSEDumper
    wire_translator: Any
    t0: float
    # Optional public Responses WebSocket transport metadata.  Kept off the
    # request body so generic providers never see Tofu control fields.
    responses_transport: str = 'sse'
    responses_state_key: str = ''
    responses_profile: str = ''
    tool_search_backend: str = ''
    programmatic_backend: str = ''
    multi_agent_backend: str = ''


def activate_native_tool_search_fallback(
    status_code: int,
    error_text: str,
    *,
    plan: RequestPlan,
    canonical_body: dict,
) -> bool:
    """Switch a rejected native discovery request to local on its retry.

    Returns ``True`` only for request-shape errors that explicitly mention
    hosted Tool Search fields.  Unrelated 400/404/422 responses retain their
    ordinary classification, so this cannot mask a bad prompt or model id.
    """
    if plan.tool_search_backend not in ('native_openai', 'native_anthropic'):
        return False
    if status_code not in (400, 404, 422):
        return False
    lower = str(error_text or '').casefold()
    signals = (
        'tool_search', 'tool search', 'defer_loading', 'deferred tool',
        'tool_search_tool_bm25', 'tool namespace',
    )
    if not any(signal in lower for signal in signals):
        return False
    canonical_body['_force_local_tool_search'] = True
    logger.warning('Native Tool Search rejected by provider (HTTP %d); '
                   'retrying with local discovery', status_code)
    return True


def activate_native_orchestration_fallback(
    status_code: int,
    error_text: str,
    *,
    plan: RequestPlan,
    canonical_body: dict,
) -> bool:
    """Retry an explicitly rejected native orchestration field locally.

    This is deliberately narrower than a generic HTTP fallback: only request-
    shape statuses and unmistakable PTC/Multi-agent field names qualify. A bad
    model, prompt, token limit, or unrelated schema therefore keeps its normal
    error classification instead of being hidden by a local retry.
    """
    if status_code not in (400, 404, 422):
        return False
    lower = str(error_text or '').casefold()
    changed: list[str] = []

    ptc_signals = (
        'programmatic_tool_calling', 'programmatic tool calling',
        'allowed_callers',
    )
    if (plan.programmatic_backend == 'native_openai'
            and any(signal in lower for signal in ptc_signals)
            and bool(canonical_body.get('_programmatic_eligible_tools'))):
        canonical_body['_force_local_programmatic'] = True
        changed.append('PTC')

    multi_agent_signals = (
        'multi_agent', 'multi-agent', 'max_concurrent_subagents',
        'responses_multi_agent',
    )
    if (plan.multi_agent_backend == 'native_openai'
            and any(signal in lower for signal in multi_agent_signals)):
        from lib.swarm.routing import catalog_has_spawn_agents
        authority = canonical_body.get('_executable_tool_catalog')
        if catalog_has_spawn_agents(authority):
            canonical_body['_force_local_multi_agent'] = True
            changed.append('Multi-agent')

    if not changed:
        return False
    logger.warning(
        'Native %s rejected by provider (HTTP %d); retrying with local '
        'orchestration', ' + '.join(changed), status_code)
    return True


def prepare_request(body, *, attempt=0, log_prefix='', api_key=None,
                    base_url=None, extra_headers=None,
                    api_protocol='openai', oauth='') -> RequestPlan:
    """Identical pre-flight for both transports.

    Mutates ``body`` in place (cache breakpoints, internal-key stripping,
    wire-protocol translation) exactly as the inline code did, then returns
    the plan.
    """
    # Keep the canonical task body intact across transport retries and
    # provider failover.  The tool-search surface below is protocol-specific;
    # reusing an already-trimmed wire body would silently lose the immutable
    # executable catalog on the next attempt.
    body = dict(body)
    _request_activity_sink = body.get('_request_activity_sink')
    if not callable(_request_activity_sink):
        _request_activity_sink = None

    def _provider_tool_name(tool):
        if not isinstance(tool, dict):
            return ''
        function = tool.get('function')
        if isinstance(function, dict):
            return str(function.get('name') or '')
        return str(tool.get('name') or '')

    # Read the latch key NON-destructively and keep it on the body for the
    # WHOLE task life. The streaming retry loop re-feeds the SAME body dict to
    # this function on every 429/503 attempt (see lib/llm/stream.py:62); popping
    # _task_id on attempt 1 made attempt 2+ fall back to the live global
    # CACHE_EXTENDED_TTL, flipping the cache_control ttl AND the beta header
    # below → a different Anthropic cache key → full prefix miss. _task_id must
    # NOT reach the gateway on the OpenAI path (raw body is serialized), so it
    # is stripped at that serialization boundary instead (see below). The
    # Anthropic path rebuilds the body from an allowlist, so it never leaks.
    _task_id_for_latch = body.get('_task_id', '')
    _responses_transport_requested = str(
        body.get('_responses_transport') or 'sse').strip().lower()
    _responses_feature_profile = str(
        body.get('_responses_feature_profile')
        or 'compatible').strip().lower()
    _responses_state_key = str(body.get('_task_id') or '')

    # Resolve discovery at the last common boundary before cache markers and
    # protocol translation.  Authorization continues to use the task-owned
    # executable catalog in the dispatch pipeline; this only controls what the
    # model sees on the wire.
    _executable_catalog = body.get('_executable_tool_catalog')
    _tool_search_backend = ''
    _required_direct_names: set[str] = set()
    if isinstance(_executable_catalog, list):
        from lib.tools.gateway import (
            CODE_CORE_DIRECT_TOOL_NAMES,
            full_wire_tools,
            local_wire_tools,
            resolve_tool_search_backend,
        )
        if body.get('_force_local_tool_search'):
            _tool_search_backend = 'local'
        elif body.get('_tool_search_fail_open'):
            _tool_search_backend = 'full'
        else:
            _tool_search_backend = resolve_tool_search_backend(
                body.get('_tool_search_mode') or 'auto',
                protocol=api_protocol, model=body.get('model') or '',
                responses_profile=_responses_feature_profile,
                base_url=base_url or '', oauth=oauth or '',
                capabilities=body.get('_tool_search_capabilities'))
        body['_resolved_tool_search_backend'] = _tool_search_backend
        _pins = list(body.get('_frontend_selected_tool_names') or ())
        _pin_set = {str(name) for name in _pins}
        _schema_budget = int(body.get('_tool_schema_budget_tokens') or 0)
        _choice = body.get('tool_choice')
        if isinstance(_choice, dict):
            _choice_function = _choice.get('function')
            if isinstance(_choice_function, dict) \
                    and _choice_function.get('name'):
                _pin_set.add(str(_choice_function['name']))
                _required_direct_names.add(str(_choice_function['name']))

        def _wire_catalog_tool(tool):
            if not isinstance(tool, dict):
                return False
            fn = tool.get('function')
            name = str((fn.get('name') if isinstance(fn, dict) else '')
                       or tool.get('name') or '')
            # MCP has already been searched by Tofu before this boundary.
            # Only the task-sticky active MCP schemas go to any provider;
            # inactive MCP tools remain in ``_executable_tool_catalog`` for
            # authorization and direct-name compatibility.
            return not name.startswith('mcp__') or name in _pin_set

        # Execution authority and prompt visibility are intentionally distinct.
        # The former remains live so an enabled tool can be called by name even
        # when it was not searched; the latter is this round's model-visible
        # projection and is the normal source for provider tool schemas.
        _stable_wire_catalog = body.get('_tool_wire_catalog')
        if isinstance(_stable_wire_catalog, list):
            _wire_catalog = list(_stable_wire_catalog)
        else:
            # Direct boundary callers derive visibility from the same
            # canonical authority when no narrower wire projection exists.
            _wire_catalog = [tool for tool in _executable_catalog
                             if _wire_catalog_tool(tool)]
        if _tool_search_backend == 'full' and body.get(
                '_tool_search_fail_open'):
            # A local retrieval failure is the one intentional exception: make
            # the executable directory visible so availability wins over cache.
            _wire_catalog = [tool for tool in _executable_catalog
                             if _wire_catalog_tool(tool)]
        _wire_names = {
            str(((tool.get('function') or {}).get('name')
                 if isinstance(tool.get('function'), dict)
                 else tool.get('name')) or '')
            for tool in _wire_catalog if isinstance(tool, dict)
        }
        _code_specific_floor = CODE_CORE_DIRECT_TOOL_NAMES - {'read_files'}
        if _wire_names & _code_specific_floor:
            _required_direct_names.update(
                CODE_CORE_DIRECT_TOOL_NAMES & _wire_names)
        if _tool_search_backend == 'local':
            body['tools'] = local_wire_tools(
                _wire_catalog,
                discovery_policy_by_name=body.get(
                    '_tool_discovery_policy_by_name'),
                discovery_catalog_size=body.get(
                    '_tool_search_catalog_size'),
                searchable_count=body.get('_tool_searchable_count'),
                include_search=True,
                schema_budget_tokens=_schema_budget,
                model=body.get('model') or '',
                priority_names=_pin_set,
                required_names=_required_direct_names,
                # PTC and multi-agent projection still follow. Reserve any
                # needed discovery gateway now, but fit exactly once below.
                apply_schema_budget=False,
                on_tool_isolated=_request_activity_sink)
        else:
            # Native discovery receives the complete catalog and lets the
            # provider defer searchable tools.  ``off`` also gets the full
            # catalog, without a discovery primitive.
            body['tools'] = full_wire_tools(_wire_catalog)
        if _tool_search_backend == 'native_anthropic':
            body['_anthropic_native_tool_search'] = True
        body['_frontend_selected_tool_names'] = _pins

    # Programmatic Tool Calling: resolve the per-request backend at the same
    # last common boundary (mirrors the Tool Search dual-backend precedent).
    # ``off`` leaves the wire untouched; ``native_openai`` lets the Responses
    # converter attach the hosted-only fields; ``local`` projects the
    # ``execute_tools`` gateway schema so ANY tool-capable wire (any protocol,
    # gateway, or OAuth profile) gets the full ToolScript surface — there is
    # no model-size split.  Only the explicit ``TOFU_PTC_TIER=batch``
    # operator/benchmark override strips the ``program`` parameter.
    from lib.tools.programmatic import (
        ACTIVE_PROGRAMMATIC_MODES,
        resolve_programmatic_backend,
    )
    _requested_ptc = str(
        body.get('_programmatic_tool_calling') or 'off').strip().lower()
    if (body.get('_force_local_programmatic')
            and _requested_ptc in ACTIVE_PROGRAMMATIC_MODES
            and bool(body.get('_programmatic_eligible_tools'))):
        # Nested local-Swarm workers and a retry after an explicit native-field
        # rejection keep the program inside the application. This preserves
        # one authority/catalog and avoids nesting provider continuations.
        _ptc_backend = 'local'
    else:
        _ptc_backend = resolve_programmatic_backend(
            _requested_ptc,
            protocol=api_protocol, model=body.get('model') or '',
            responses_profile=_responses_feature_profile,
            base_url=base_url or '', oauth=oauth or '',
            eligible_present=bool(body.get('_programmatic_eligible_tools')))
    body['_resolved_programmatic_backend'] = _ptc_backend
    _orchestration_sink = body.get('_tool_orchestration_decision_sink')
    _record_orchestration_v2_evidence = (
        isinstance(_orchestration_sink, dict)
        and body.get('_tool_orchestration_policy_version')
        == 'tool-orchestration/v2')
    if isinstance(_orchestration_sink, dict):
        _orchestration_sink['programmaticBackend'] = _ptc_backend
        if _record_orchestration_v2_evidence and _ptc_backend != 'off':
            from lib.orchestration_adoption import (
                record_orchestration_projection)
            record_orchestration_projection(
                _orchestration_sink,
                lane='programmatic', backend=_ptc_backend)
    if _ptc_backend == 'local' and isinstance(body.get('tools'), list):
        from lib.tools.gateway import ptc_local_wire_tools
        body['tools'] = ptc_local_wire_tools(
            body['tools'], tier=body.get('_programmatic_tier') or 'program',
            eligible=body.get('_programmatic_eligible_tools') or ())

    # Multi-agent is an independent control plane.  Resolve it after PTC so
    # both lanes may be active in one request: local models see execute_tools
    # plus a read-only spawn_agents projection, while public GPT-5.6 Responses
    # receives the two native extensions together.  Native projection removes
    # spawn_agents from the visible surface to avoid offering two competing
    # control planes; execution authority remains task-owned and unchanged.
    from lib.swarm.routing import (
        catalog_has_spawn_agents,
        project_multi_agent_wire_tools,
        resolve_multi_agent_backend,
    )
    _multi_agent_authority_catalog = (
        _executable_catalog if isinstance(_executable_catalog, list)
        else body.get('tools') if isinstance(body.get('tools'), list)
        else [])
    _local_swarm_available = catalog_has_spawn_agents(
        _multi_agent_authority_catalog)
    if (body.get('_force_local_multi_agent')
            and str(body.get('_multi_agent_mode') or '').lower()
            == 'read_only'):
        _multi_agent_backend = (
            'local_swarm' if _local_swarm_available else 'off')
    else:
        _multi_agent_backend = resolve_multi_agent_backend(
            body.get('_multi_agent_mode') or 'off',
            protocol=api_protocol, model=body.get('model') or '',
            responses_profile=_responses_feature_profile,
            base_url=base_url or '', oauth=oauth or '',
            local_swarm_available=_local_swarm_available)
    body['_resolved_multi_agent_backend'] = _multi_agent_backend
    if isinstance(_orchestration_sink, dict):
        _orchestration_sink['multiAgentBackend'] = _multi_agent_backend
        if (_record_orchestration_v2_evidence
                and _multi_agent_backend != 'off'):
            from lib.orchestration_adoption import (
                record_orchestration_projection)
            record_orchestration_projection(
                _orchestration_sink,
                lane='multi_agent', backend=_multi_agent_backend)
    if isinstance(body.get('tools'), list):
        body['tools'] = project_multi_agent_wire_tools(
            body['tools'], authority_catalog=_multi_agent_authority_catalog,
            backend=_multi_agent_backend,
            stage=body.get('_multi_agent_stage') or '',
            max_concurrent_agents=(body.get(
                '_multi_agent_max_concurrent_agents')
                or body.get('_multi_agent_max_concurrent_subagents') or 3),
            programmatic_workers=(
                _ptc_backend in ('local', 'native_openai')))

    # PTC and Multi-agent may append schemas after Tool Search projection. Fit
    # an explicit, model-neutral local cost target exactly once at this final
    # common projection boundary.
    _final_schema_budget = int(body.get('_tool_schema_budget_tokens') or 0)
    _budget_dropped_names: list[str] = []
    _budget_compacted_names: list[str] = []
    if (_final_schema_budget and _tool_search_backend == 'local'
            and isinstance(body.get('tools'), list)):
        from lib.tools.gateway import fit_tool_schema_budget
        _before_budget = {
            _provider_tool_name(tool): tool for tool in body['tools']
            if _provider_tool_name(tool)
        }
        _priority_names = set(body.get('_frontend_selected_tool_names') or ())
        _required_names = set(_required_direct_names)
        if _multi_agent_backend != 'off':
            # Local Swarm is one lifecycle capability, not three independent
            # optional schemas.  Keeping only ``spawn_agents`` under budget
            # pressure can launch work that the model can neither wait for nor
            # retrieve without paying an avoidable discovery round.
            from lib.swarm.tools import SWARM_CONTROL_TOOL_NAMES
            _required_names.update(SWARM_CONTROL_TOOL_NAMES)
        body['tools'] = fit_tool_schema_budget(
            body['tools'], budget_tokens=_final_schema_budget,
            model=body.get('model') or '', priority_names=_priority_names,
            required_names=_required_names,
            on_tool_isolated=_request_activity_sink)
        _after_budget = {
            _provider_tool_name(tool): tool for tool in body['tools']
            if _provider_tool_name(tool)
        }
        _budget_dropped_names = [
            name for name in _before_budget if name not in _after_budget]
        _budget_compacted_names = [
            name for name, schema in _before_budget.items()
            if name in _after_budget and _after_budget[name] != schema]

    # Wire-contract invariant at the LAST common boundary: whatever shape the
    # tools array arrived in (registry assembly, latched catalogs, headless
    # custom tools, rescue re-dispatch), every element that reaches a provider
    # is an object with a usable name, and function tools carry
    # ``type='function'``.  A single ``None`` used to crash
    # ``add_cache_breakpoints`` (2026-08-19 task c9aba5d0 FATAL) and 400
    # Gemini ("'tools' array element to be an object"); a missing ``type``
    # 400s kimi ("unknown tool type: ").  Clean arrays pass through by
    # identity, so the prompt-cache hot path is untouched.
    if isinstance(body.get('tools'), list):
        from lib.tools.gateway import sanitize_wire_tools
        body['tools'] = sanitize_wire_tools(
            body['tools'], log_prefix=log_prefix,
            on_tool_isolated=_request_activity_sink)
        if not body['tools']:
            # Some OpenAI-compatible providers reject an empty tools array,
            # and a tool_choice without a surviving tool is necessarily
            # invalid. A request whose only malformed tool was isolated must
            # still be able to continue as an ordinary model request.
            body.pop('tools', None)
            body.pop('tool_choice', None)

    add_cache_breakpoints(body, log_prefix, api_protocol=api_protocol)

    # Auto-inject extended cache TTL beta header for Claude
    if is_claude(body.get('model', '')):
        if _task_id_for_latch:
            from lib.tasks_pkg.cache_tracking._ttl import latch_extended_ttl
            _use_ext_ttl = latch_extended_ttl(_task_id_for_latch)
        else:
            _use_ext_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        if _use_ext_ttl:
            if extra_headers is None:
                extra_headers = {}
            _existing_beta = extra_headers.get('anthropic-beta', '')
            _ttl_beta = 'extended-cache-ttl-2025-04-11'
            if _ttl_beta not in _existing_beta:
                if _existing_beta:
                    extra_headers['anthropic-beta'] = f'{_existing_beta},{_ttl_beta}'
                else:
                    extra_headers['anthropic-beta'] = _ttl_beta

    # Subscription-OAuth slot: swap in a live token + client-identity headers,
    # and (for Claude) prepend the mandatory identity system block — all BEFORE
    # the body translation below reads messages / builds headers.
    if oauth:
        from lib.oauth.outbound import resolve_oauth_request
        api_key, extra_headers, body = resolve_oauth_request(oauth, body, extra_headers)

    # Wire-protocol translation at the HTTP boundary. SINGLE GATE:
    # ``api_protocol`` (provider config; the dispatcher coerces
    # oauth='codex' slots to 'responses' — the Codex backend speaks ONLY
    # Responses). The canonical OpenAI body is converted per protocol and
    # a stateful translator rides the plan to convert the SSE stream back.
    wire_translator = None
    _responses_profile = ''
    _responses_transport = 'sse'
    if api_protocol == 'responses':
        from lib.llm.responses_outbound import (
            ResponsesSSETranslator,
            openai_body_to_responses,
            responses_url,
        )
        # oauth='codex' selects the Codex dialect (instructions/include/
        # dropped sampling params); every other Responses provider gets
        # the generic profile.
        _profile = 'codex' if oauth == 'codex' else 'default'
        _responses_profile = _profile
        body, _resp_reverse = openai_body_to_responses(
            body, profile=_profile, stream=True)
        wire_translator = ResponsesSSETranslator(model=body.get('model', ''))
        # Truncated (64-char) tool names echo back from the model — the
        # per-request reverse map restores them before tool dispatch.
        wire_translator.tool_name_reverse = _resp_reverse
        url = responses_url(base_url)
        _responses_model = str(body.get('model') or '').lower()
        from lib.model_info._openai_gpt56 import is_official_gpt56_model
        if (_profile == 'default'
                and _responses_feature_profile == 'openai'
                and is_official_gpt56_model(_responses_model)
                and _responses_transport_requested == 'websocket'
                and _responses_state_key):
            _responses_transport = 'websocket'
        logger.debug('%s [Responses] Translated request for Responses API '
                     '(profile=%s)', log_prefix, _profile)
    elif api_protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            AnthropicSSETranslator, anthropic_messages_url,
            openai_body_to_anthropic,
        )
        _model_name = body.get('model', '')
        body = openai_body_to_anthropic(body)
        wire_translator = AnthropicSSETranslator(model=_model_name)
        url = anthropic_messages_url(base_url)
        if oauth == 'claude':
            from lib.oauth.outbound import apply_claude_cloak, claude_oauth_url
            url = claude_oauth_url(url)
            # 2026 cloaking (billing header / static prompt / tool rename) at
            # the Anthropic-body boundary; the per-request reverse map rides
            # the translator so response tool names are restored.
            body, _cloak_reverse = apply_claude_cloak(body)
            wire_translator.tool_name_reverse = _cloak_reverse
        logger.debug('%s [Anthropic] Translated request for Messages API', log_prefix)
    else:
        # OpenAI path serialises `body` verbatim (session.post(json=body)), so
        # the internal latch key must be removed HERE — the single serialization
        # boundary — rather than popped early (which broke the retry-stable
        # latch, see above). The Anthropic/Responses branches rebuild `body`
        # from an allowlist that never included internal underscore keys, so this only
        # matters here.
        # Build a wire-only top-level envelope instead of popping from the
        # caller's canonical dict. ``stream_chat`` reuses that dict on a
        # transport retry; destructive pops made attempt 2 lose the task TTL
        # latch (and would also lose the Responses cache namespace if a caller
        # switched protocol during failover). Nested messages/tools stay
        # shared intentionally: add_cache_breakpoints has already normalized
        # them and is idempotent on the next attempt.
        _internal_keys = {
            '_task_id', '_conv_id', '_working_set_tokens',
            '_gpt56_breakpoint_mode', '_programmatic_tool_calling',
            '_programmatic_stage', '_programmatic_tier',
            '_programmatic_eligible_tools', '_programmatic_serial_chain',
            '_resolved_programmatic_backend', '_force_local_programmatic',
            '_tool_search_mode', '_frontend_selected_tool_names',
            '_tool_schema_budget_tokens',
            '_tool_namespace_by_name', '_responses_transport',
            '_executable_tool_catalog',
            '_tool_wire_catalog',
            '_tool_discovery_policy_by_name',
            '_tool_search_catalog_size', '_tool_searchable_count',
            '_tool_search_capabilities', '_resolved_tool_search_backend',
            '_anthropic_native_tool_search',
            '_force_local_tool_search', '_tool_search_fail_open',
            '_reasoning_mode', '_text_verbosity', '_image_detail',
            '_multi_agent_mode', '_multi_agent_stage',
            '_multi_agent_max_concurrent_agents',
            '_multi_agent_max_concurrent_subagents',
            '_resolved_multi_agent_backend',
            '_force_local_multi_agent',
            '_tool_orchestration_policy_version',
            '_tool_orchestration_composition', '_safety_identifier',
            '_tool_orchestration_decision_sink',
            '_request_activity_sink',
            '_responses_feature_profile',
        }
        body = {key: value for key, value in body.items()
                if key not in _internal_keys}
        _message_sidecars = frozenset({
            '_responses_items', '_anthropic_content_blocks',
        })
        if any(isinstance(msg, dict)
               and any(key in msg for key in _message_sidecars)
               for msg in body.get('messages') or ()):
            body['messages'] = [
                ({key: value for key, value in msg.items()
                  if key not in _message_sidecars}
                 if isinstance(msg, dict) else msg)
                for msg in body.get('messages') or ()
            ]
        url = f'{base_url.rstrip("/")}/chat/completions' if base_url else chat_url()

    attempt_tag = f' (attempt {attempt+1})' if attempt > 0 else ''
    if log_prefix:
        _wire_item_count = len(body.get('messages') or body.get('input') or [])
        logger.debug('%s%s POST %s msgs=%d tools=%s', log_prefix, attempt_tag, url,
                     _wire_item_count, 'yes' if body.get('tools') else 'no')

    trace_id = uuid.uuid4().hex
    if api_protocol == 'anthropic':
        from lib.llm.anthropic_outbound import anthropic_headers
        hdrs = anthropic_headers(api_key, extra_headers)
        if oauth == 'claude':
            # Subscription tokens are rejected on Authorization: Bearer
            # (401 since 2026); the token must ride x-api-key only.
            hdrs.pop('Authorization', None)
    else:
        hdrs = headers()
        if api_key:
            hdrs['Authorization'] = f'Bearer {api_key}'
        if extra_headers:
            hdrs.update(extra_headers)
    hdrs['M-TraceId'] = trace_id
    if (api_protocol == 'responses'
            and isinstance(body.get('multi_agent'), dict)
            and body['multi_agent'].get('enabled')):
        beta_key = next((key for key in hdrs
                         if key.lower() == 'openai-beta'), 'OpenAI-Beta')
        beta = str(hdrs.get(beta_key) or '')
        marker = 'responses_multi_agent=v1'
        if marker not in beta:
            hdrs[beta_key] = f'{beta},{marker}' if beta else marker

    if _request_activity_sink is not None:
        try:
            from lib.tools.gateway import (
                tool_schema_fingerprint, tool_schema_tokens)
            _provider_tools = (
                body.get('tools') if isinstance(body.get('tools'), list) else [])
            _request_activity_sink({
                'kind': 'wire_projection',
                'model': body.get('model') or '',
                'backend': _tool_search_backend,
                'toolNames': [
                    name for name in map(_provider_tool_name, _provider_tools)
                    if name
                ],
                'toolCount': len(_provider_tools),
                'schemaTokens': tool_schema_tokens(
                    _provider_tools, model=body.get('model') or ''),
                # Exact, cache-relevant provider projection fingerprint.  Names
                # and token counts can stay unchanged while a description or
                # parameter byte changes; this bounded digest makes that drift
                # visible without persisting the full schema.
                'schemaFingerprint': tool_schema_fingerprint(_provider_tools),
                'schemaBudgetTokens': _final_schema_budget,
                'budgetDroppedNames': _budget_dropped_names,
                'compactedNames': _budget_compacted_names,
                'executableToolCount': (
                    len(_executable_catalog)
                    if isinstance(_executable_catalog, list) else len(_provider_tools)),
            })
        except Exception:
            logger.warning('%s provider wire projection diagnostic failed',
                           log_prefix, exc_info=True)

    if log_prefix:
        logger.debug('%s M-TraceId=%s', log_prefix, trace_id)

    # This is the one request-attempt boundary used by transport, semantic
    # progress and final evidence. Wall time remains for logs only.
    t0 = time.monotonic()
    raw_dumper = RawSSEDumper(body.get('model', ''), trace_id, body)
    raw_dumper.start()

    # ── Wire fingerprint (cache-miss traceability) ──
    # This is the ONLY point that sees the FINAL, post-translation messages
    # exactly as they go on the wire (after add_cache_breakpoints AND, on the
    # anthropic path, openai_body_to_anthropic). Canonicalise them into an
    # envelope-agnostic fingerprint and stash it on the RawSSEDumper (which
    # travels into SSEAccumulator → finalize, where it is relayed into `usage`
    # like trace_id). Stashing it on the dumper — NOT on `body` — keeps the
    # ephemeral fingerprint OFF the wire (body is what requests/httpx serialise).
    # detect_cache_break then PROVES a server-side miss (bytes identical) vs.
    # names a client-caused culprit. Best-effort: never block a request.
    raw_dumper.wire_fp = None
    raw_dumper.wire_static = ''
    raw_dumper.wire_system = None
    raw_dumper.wire_markers = None
    raw_dumper.wire_bytes = None
    raw_dumper.wire_field_bytes = None
    raw_dumper.wire_region = None
    try:
        from lib.tasks_pkg.wire_fingerprint import (
            canonical_messages, marker_signature, static_prefix_hash,
            system_fingerprint, wire_byte_field_prefix, wire_byte_prefix,
            wire_byte_region,
        )
        _wire_items = body.get('messages') or body.get('input') or []
        raw_dumper.wire_fp = canonical_messages(_wire_items)
        raw_dumper.wire_static = static_prefix_hash(_wire_items)
        # TRUE-byte prefix: hash the ACTUAL serialized bytes per message (only
        # cache_control stripped). canonical_messages is lossy (drops
        # reasoning_details, collapses str↔block, canonicalises arg order), so
        # "canonical identical" does NOT prove "wire bytes identical". This lets
        # detect_cache_break REFUSE a false "byte-identical eviction" claim when
        # the real bytes diverged (reasoning_details rebuild / same-role merge /
        # protocol switch) — see wire_byte_prefix's docstring.
        raw_dumper.wire_bytes = wire_byte_prefix(_wire_items)
        # FIELD-GRANULAR true bytes: names the EXACT top-level field that
        # flipped on a canonical-invisible <bytes> divergence (reasoning_details
        # rebuild / tool_calls arg re-serialization / content / field-order),
        # so detect_cache_break can log the proven field instead of only the
        # message. See wire_byte_field_prefix.
        raw_dumper.wire_field_bytes = wire_byte_field_prefix(
            _wire_items)
        # TRUE-byte hash of the HOISTED system + tools region. system_fingerprint
        # is ITSELF lossy (runs _text_of + sort_keys on params), so a system
        # BLOCK REORDER / wrapping flip / per-turn re-serialization — the
        # highest-probability suspect on the Anthropic path, where charter /
        # board / peer-status / relevant_memories are injected fresh each turn —
        # is invisible to it. This hashes the real serialized bytes so that
        # divergence can't be laundered into "eviction". See wire_byte_region.
        raw_dumper.wire_region = wire_byte_region(
            body.get('system'), body.get('tools'))
        # Capture WHERE the cache_control breakpoints sit — canonical_messages
        # deliberately strips them, so a miss caused purely by a breakpoint
        # being LOST in translation (byte-identical content) would otherwise be
        # mislabeled "server-side PROVEN". detect_cache_break folds this in.
        raw_dumper.wire_markers = marker_signature(body)
        # Also fingerprint the HOISTED system block + tools. On the Anthropic
        # path these live OUTSIDE body['messages'] (openai_body_to_anthropic
        # lifts system to the top-level field), so canonical_messages is blind
        # to them — a per-turn system change (digest/charter/board) was
        # laundered into a false "server-side PROVEN" verdict. This closes that
        # blind spot.
        raw_dumper.wire_system = system_fingerprint(
            body.get('system'), body.get('tools'))
    except Exception as _wfe:
        logger.debug('%s wire fingerprint capture failed: %s', log_prefix, _wfe)

    # Diagnostic byte-probe (default OFF): dump the exact post-translation body
    # for a targeted conv so a raw-byte round-over-round diff can settle whether
    # a "server-side PROVEN" miss is actually a client-caused prefix mutation.
    # Also capture the ROUTING fingerprint — key discriminator / endpoint /
    # final anthropic-beta — so the diff can tell a body-byte flip from a
    # cache-namespace (key/endpoint) change on a byte-identical prefix.
    _routing = None
    try:
        import hashlib as _hashlib
        _key_hash = ''
        if api_key:
            _key_hash = _hashlib.sha256(
                ('tofu-cache-probe:' + str(api_key)).encode('utf-8')
            ).hexdigest()[:12]
        _sticky = {}
        try:
            from lib.llm_dispatch.conv_affinity import get_pick_decision
            _sticky = get_pick_decision() or {}
        except Exception as _se:
            logger.debug('%s cache-probe sticky capture failed: %s', log_prefix, _se)
        _routing = {
            'url': url,
            'base_url': base_url or '',
            'key_hash': _key_hash,           # salted, truncated — NOT the secret
            'anthropic_beta': (hdrs.get('anthropic-beta', '')
                               if isinstance(hdrs, dict) else ''),
            'trace_id': trace_id,
            'attempt': attempt,
            'api_protocol': api_protocol,
            # Sticky-routing decision (WHY the key is what it is): distinguishes
            # a soft-fallback-under-cooldown flip (affinity_fell_back=True) from
            # affinity never engaging. {preferred_key_hash, chosen_key_hash,
            # affinity_fell_back, cooldown_remaining_s}.
            'sticky': _sticky,
        }
    except Exception as _rfe:
        logger.debug('%s cache-probe routing capture failed: %s', log_prefix, _rfe)

    # ── ALWAYS-ON cache-namespace routing fingerprint (relayed into usage) ──
    # The byte-probe above is default-OFF, so historically the routing captured
    # here reached NOTHING at runtime and the cache-miss verdict was blind to a
    # key/beta/endpoint flip — mislabeling a client cache-namespace switch as a
    # server-side miss. Distil _routing down to the three attributes that
    # DETERMINE the gateway cache namespace and stash them on the dumper so
    # SSEAccumulator.finalize relays them into `usage['_wire_routing']` UNCONDI-
    # TIONALLY (like _wire_fp). detect_cache_break diffs it round-over-round and
    # NAMES a namespace switch instead of blaming the gateway. Best-effort: a
    # capture failure leaves wire_routing=None → the detector's diff is inert.
    raw_dumper.wire_routing = None
    if _routing is not None:
        try:
            from lib.tasks_pkg.wire_fingerprint import routing_fingerprint
            raw_dumper.wire_routing = routing_fingerprint(
                key_hash=_routing.get('key_hash', ''),
                anthropic_beta=_routing.get('anthropic_beta', ''),
                endpoint=_routing.get('url', ''))
        except Exception as _rfpe:
            logger.debug('%s routing fingerprint build failed: %s',
                         log_prefix, _rfpe)

    _maybe_dump_cache_probe(body, _task_id_for_latch, log_prefix, routing=_routing)

    return RequestPlan(url=url, hdrs=hdrs, body=body, trace_id=trace_id,
                       raw_dumper=raw_dumper, wire_translator=wire_translator,
                       t0=t0, responses_transport=_responses_transport,
                       responses_state_key=_responses_state_key,
                       responses_profile=_responses_profile,
                       tool_search_backend=_tool_search_backend,
                       programmatic_backend=_ptc_backend,
                       multi_agent_backend=_multi_agent_backend)


def classify_status_error(status_code, err_text, *, body, log_prefix, raw_dumper):
    """Shared non-200 handling. Caller reads the body text per-transport.

    Always raises (via ``_classify_http_error``) — never returns normally
    when ``status_code != 200``.
    """
    # _ERR_BODY_LIMIT (not 800): the 800-char cap amputated the JSON envelope
    # mid-way, so summarize_error_body (called inside _classify_http_error)
    # failed to parse and leaked the raw envelope into the retry HUD, and the
    # gateway's tail diagnostics (ext.error.source/service/stage + request id)
    # were lost from error.log. repair_mojibake is NOT applied here: both
    # callers hand in already-decoded text (stream.py via decode_error_body,
    # astream.py via its own repair wrap), so the repair boundary stays single.
    err_msg = f'API HTTP {status_code}: {err_text[:_ERR_BODY_LIMIT]}'
    if raw_dumper.enabled:
        raw_dumper.line(f'[HTTP-{status_code}] {err_text[:_ERR_BODY_LIMIT]}')
    _classify_http_error(status_code, err_msg, body.get('model', ''),
                         log_prefix, max_tokens=body.get('max_tokens', 0))


def _wire_tool_names(tools) -> set[str]:
    """Extract callable names from every supported wire-tool shape.

    Responses Tool Search nests deferred functions inside ``namespace.tools``;
    treating only the namespace's own name as callable would falsely report a
    successfully searched function as hallucinated.
    """
    names: set[str] = set()
    for tool in tools or ():
        if not isinstance(tool, dict):
            continue
        if tool.get('type') == 'namespace':
            names.update(_wire_tool_names(tool.get('tools') or ()))
            continue
        func = tool.get('function')
        if isinstance(func, dict) and func.get('name'):
            names.add(str(func['name']))
        elif tool.get('type') == 'function' and tool.get('name'):
            names.add(str(tool['name']))
        elif (tool.get('name') and tool.get('type') not in (
                'tool_search', 'programmatic_tool_calling')):
            # Anthropic-native function schema.
            names.add(str(tool['name']))
    return names


class SSEAccumulator:
    """Transport-agnostic SSE chunk parser + assistant-message builder.

    Usage::

        acc = SSEAccumulator(body, trace_id, raw_dumper, wire_translator,
                             t0, log_prefix=..., on_thinking=..., ...)
        framer = SSEFramer()
        for raw_chunk in transport_bytes():
            if abort_check and abort_check():
                acc.mark_aborted(); break
            for event in framer.feed(raw_chunk):
                if acc.feed_event(event):   # True → saw [DONE]
                    break
        acc.fire_final_tool_callback()
        msg, finish_reason, usage = acc.finalize(resp_trace=...)
    """

    def __init__(self, body, trace_id, raw_dumper, wire_translator, t0, *,
                 url='', log_prefix='', on_thinking=None, on_content=None,
                 on_tool_call_ready=None, on_reasoning_progress=None,
                 on_actionable_output=None, progress=None):
        self.body = body
        self.trace_id = trace_id
        self.raw_dumper = raw_dumper
        self.wire_translator = wire_translator
        self.t0 = t0
        self.url = url
        self.log_prefix = log_prefix
        self.on_thinking = on_thinking
        self.on_content = on_content
        self.on_tool_call_ready = on_tool_call_ready
        # Transport-liveness callbacks consume only normalized semantic
        # events. Provider comments, signatures, and metadata never reach
        # these seams, so all wire protocols share one timeout meaning.
        self.on_reasoning_progress = on_reasoning_progress
        self.on_actionable_output = on_actionable_output
        self.progress = progress or StreamProgress(0)

        self.content = ''
        self.thinking_text = ''
        self.thinking_signature = ''
        self.tool_calls_acc: dict = {}
        # No provider finish reason exists until a terminal choice frame says
        # so.  The historical tuple adapter still exposes ``stop`` to callers
        # that unpack the result, but semantic completion never comes from a
        # constructor default again.
        self.finish_reason: str | None = None
        self.usage: Optional[dict] = None
        self.saw_done = False
        self.saw_finish_reason = False
        self.chunk_count = 0
        self.aborted_by_client = False
        self.semantic_progress_timeout_s = 0.0
        self.semantic_progress_diagnostics: dict = {}

        self._mm_mode = is_minimax(body.get('model', ''))
        self._mm_in_think = False
        self._mm_buf = ''
        self._consecutive_parse_errors = 0
        self._malformed_frame_count = 0
        self._progress_tool_slots: set[str] = set()
        # ── tool_calls wire-shape OBSERVATION counters (pure diagnostics) ──
        # These exist to settle a 2026-07-27 open question: a concatenated
        # tool name (``read_filesrun_command``) reached tool_dispatch, and two
        # explanations were equally consistent — (a) a slot collision in the
        # accumulator below (``index`` defaulting to 0 for two distinct
        # calls), or (b) the model/gateway emitting the concatenated name
        # itself. The existing raw_sse_anomaly.log could NOT decide it: its
        # tool_calls samples are 100% ``toolu_bdrk_`` (bedrock line), zero
        # frames from the sankuai OpenAI-compat line where the incident
        # happened. So sample the shape here, on whatever line is live.
        # These counters change NO behaviour.
        self._tc_obs_index_absent = 0
        self._tc_obs_name_reissue = 0
        # Slot the unindexed deltas of the CURRENT call accumulate into.
        # ``None`` = no unindexed call open yet.
        self._tc_unindexed_slot = None
        # Upstream ``index`` → the internal slot its deltas currently belong to.
        # These diverge the moment an upstream reuses one index for two calls:
        # the second call gets a fresh internal slot, and EVERY later delta
        # bearing that upstream index — arguments included — must follow it.
        # Routing only the NAME would leave the second call's arguments piling
        # into the first slot, producing two dispatchable calls with swapped /
        # missing arguments (silently wrong, unlike a fused name).
        self._tc_index_map: dict = {}
        # How many times a second tool name forced a new slot instead of being
        # concatenated onto the previous one (the 2026-07-27 root cause).
        self._tc_obs_slot_split = 0

    def mark_aborted(self):
        self.aborted_by_client = True
        self.progress.mark_client_aborted()
        logger.debug('%s Stream aborted by client after %d chunks',
                     self.log_prefix, self.chunk_count)

    @property
    def has_actionable_output(self) -> bool:
        """Whether this attempt has produced deliverable text or a tool call.

        Reasoning and protocol keep-alives intentionally do not count. They
        prove the socket is alive, but they cannot advance the task or give the
        user a final answer.
        """
        return bool(self.content or self.tool_calls_acc)

    def mark_semantic_progress_timeout(
            self, timeout_s: float, *, diagnostics=None) -> None:
        """Record a semantic stall and its bounded monotonic diagnostics."""
        try:
            self.semantic_progress_timeout_s = max(
                0.0, float(timeout_s or 0))
        except (TypeError, ValueError, OverflowError):
            self.semantic_progress_timeout_s = 0.0
        source = diagnostics if isinstance(diagnostics, dict) else {}
        numeric_keys = (
            'request_elapsed_s',
            'last_progress_age_s',
            'reasoning_chars',
            'reasoning_chunks',
        )
        bounded = {}
        for key in numeric_keys:
            if key not in source:
                continue
            try:
                bounded[key] = max(0.0, float(source.get(key) or 0))
            except (TypeError, ValueError, OverflowError):
                bounded[key] = 0.0
        self.semantic_progress_diagnostics = bounded
        self.progress.mark_semantic_timeout()

    # Historical internal spelling retained while callers migrate.
    mark_no_actionable_timeout = mark_semantic_progress_timeout

    def record_malformed_frames(self, count=1, diagnostics=()) -> None:
        """Record malformed wire evidence without retaining provider data."""
        try:
            normalized_count = max(0, int(count or 0))
        except (TypeError, ValueError, OverflowError):
            normalized_count = 1
        self._malformed_frame_count += normalized_count
        self.progress.mark_malformed(normalized_count, diagnostics)

    def _notify_reasoning_progress(self, text) -> None:
        """Forward only non-blank normalized reasoning to transport policy."""
        if (not isinstance(text, str) or not text.strip()
                ):
            return
        self.progress.mark_reasoning(text)
        if self.on_reasoning_progress is None:
            return
        try:
            self.on_reasoning_progress(text)
        except Exception as error:
            logger.debug('%s reasoning-progress callback error: %s',
                         self.log_prefix, error)

    def _notify_actionable_output(self) -> None:
        """Notify the transitional actionable-output callback."""
        if self.on_actionable_output is None:
            return
        try:
            self.on_actionable_output()
        except Exception as error:
            logger.debug('%s actionable-output callback error: %s',
                         self.log_prefix, error)

    def feed_line(self, line) -> bool:
        """Process one raw SSE line. Returns True when the stream should stop.

        Mirrors the inline per-line handling exactly: dumps the raw line,
        skips non-``data:`` lines, detects ``[DONE]``, and dispatches the
        JSON chunk. Raises the same typed exceptions on SSE error objects.
        """
        self.raw_dumper.line(line if line is not None else '')
        if not line or not line.startswith('data:'):
            return False
        data_str = line[5:]
        if data_str.startswith(' '):
            data_str = data_str[1:]
        return self.feed_payload(data_str, count_event=True, dump_raw=False)

    def feed_event(self, event: SSEEvent) -> bool:
        """Consume one complete event produced by the shared byte framer."""
        if not isinstance(event, SSEEvent):
            raise TypeError('feed_event requires SSEEvent')
        self.raw_dumper.line(
            f'event: {event.event}' if event.event else 'event: message')
        return self.feed_payload(event.data, count_event=True, dump_raw=True)

    def feed_payload(
            self, data_str: str, *, count_event: bool = True,
            dump_raw: bool = True) -> bool:
        """Consume one already-decoded provider payload.

        WebSocket providers call this directly; HTTP providers arrive through
        ``SSEFramer`` + ``feed_event``. No transport fabricates a ``data:``
        line merely to reach the provider translator.
        """
        if not isinstance(data_str, str):
            self.record_malformed_frames(
                1, ('invalid_payload: provider payload was not text',))
            return False
        if dump_raw:
            self.raw_dumper.line(
                f'data: <decoded payload chars={len(data_str)}>')
        if count_event:
            self.progress.mark_sse_event()
        if data_str == '[DONE]':
            self.saw_done = True
            self.progress.mark_done()
            return True
        if not data_str.strip():
            return False
        self.chunk_count += 1

        # Non-OpenAI wire protocols (responses / anthropic) — translate
        # the payload into OpenAI chunks first, then accumulate sharedly.
        if self.wire_translator is not None:
            return self._feed_translated(data_str)

        try:
            chunk = json.loads(data_str)
        except Exception as e:
            self._consecutive_parse_errors += 1
            self.record_malformed_frames(
                1,
                (f'invalid_json: chunk={self.chunk_count} '
                 f'error={type(e).__name__}',),
            )
            logger.warning('%s ⚠ SSE event JSON parse error (chunk #%d, consecutive=%d) '
                           'model=%s trace=%s chars=%d error=%s',
                           self.log_prefix, self.chunk_count, self._consecutive_parse_errors,
                           self.body.get('model', '?'), self.trace_id,
                           len(data_str), type(e).__name__,
                           exc_info=True)
            if self._consecutive_parse_errors >= _MAX_CONSECUTIVE_PARSE_ERRORS:
                self.raw_dumper.dump_anomaly(
                    'parse_error',
                    consecutive_errors=self._consecutive_parse_errors,
                    chunk_count=self.chunk_count,
                    model=self.body.get('model', '?'),
                )
                raise RetryableAPIError(
                    f'{self._consecutive_parse_errors} consecutive SSE parse errors '
                    f'— stream appears corrupt') from e
            return False

        self._consecutive_parse_errors = 0
        self._process_openai_chunk(chunk)
        return False

    def _process_openai_chunk(self, chunk):
        """Accumulate one OpenAI-shaped chat.completion chunk dict."""
        if not isinstance(chunk, dict):
            self.record_malformed_frames(
                1, ('invalid_json_shape: provider event was not an object',))
            return
        if 'error' in chunk:
            self._handle_sse_error(chunk['error'])

        if chunk.get('usage'):
            self.usage = chunk['usage']

        choices = chunk.get('choices', [])
        if not choices:
            return

        delta = choices[0].get('delta', {})
        fr = choices[0].get('finish_reason')
        if fr:
            self.finish_reason = fr
            self.saw_finish_reason = True
            self.progress.mark_provider_finish()
        if choices[0].get('usage'):
            self.usage = choices[0]['usage']

        self._handle_delta(delta)

    def _feed_translated(self, data_str) -> bool:
        """Translate one non-OpenAI SSE payload via the wire translator.

        Handles both dict-emitting translators (ResponsesSSETranslator,
        AnthropicSSETranslator) and legacy JSON-string emitters, routing
        every translated chunk through the SAME ``_process_openai_chunk``
        path the main OpenAI path uses. Sharing that one accumulator keeps
        content / thinking / tool-call-delta accumulation,
        ``on_tool_call_ready`` firing, and ``usage`` handling byte-identical
        across every provider — the Codex path previously re-implemented
        the accumulation and, in doing so, (1) never fired
        ``on_tool_call_ready`` (no incremental multi-tool prefetch) and
        (2) gated content/thinking *accumulation* on the callback being
        present (``if _c and self.on_content``), silently dropping the
        whole response for a caller with no streaming callback.

        Returns True when the translator emits the ``[DONE]`` sentinel.
        """
        try:
            provider_event = json.loads(data_str)
        except (json.JSONDecodeError, TypeError) as error:
            self.record_malformed_frames(
                1,
                (f'invalid_json: translated provider event '
                 f'error={type(error).__name__}',),
            )
            return False
        if not isinstance(provider_event, dict):
            self.record_malformed_frames(
                1,
                ('invalid_json_shape: translated provider event was not an '
                 'object',),
            )
            return False

        for item in self.wire_translator.translate(data_str):
            if item == '[DONE]':
                self.saw_done = True
                self.progress.mark_done()
                return True
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except Exception as e:
                    self.record_malformed_frames(
                        1,
                        (f'invalid_translated_json: '
                         f'error={type(e).__name__}',),
                    )
                    logger.debug('[LLM] translated SSE chunk parse failed: %s', e)
                    continue
            self._process_openai_chunk(item)
        return False

    def _handle_sse_error(self, eo):
        """Classify an SSE-embedded error object; always raises."""
        err_text = eo.get('message', '') if isinstance(eo, dict) else str(eo)
        # This text is parsed straight from the SSE error JSON — unlike the
        # non-200 path it never passes through decode_error_body, so the
        # UPSTREAM_VENDOR double-encoding (2026-07-26) would sail through into
        # logs, the raised exception, AND the Chinese pattern matchers below
        # (which would then miss '稍后重试'/'负载较高'). Repair FIRST so both
        # display and classification see the intended text. Idempotent on
        # already-clean CJK (repair only fires when it GAINS CJK).
        err_text = repair_mojibake(err_text)
        _err_lower = err_text.lower()
        _model_id = self.body.get('model', '')
        _detected_limit = _parse_token_limit_from_error(err_text, _model_id)
        if _detected_limit:
            _learn_model_limit(_model_id, _detected_limit)
            raise ModelLimitError(
                f'SSE error (token limit): {err_text}',
                _model_id, _detected_limit,
                self.body.get('max_tokens', 0))
        if _is_prompt_too_long(err_text):
            logger.warning('%s Prompt too long detected in SSE error: %s',
                           self.log_prefix, err_text[:_ERR_BODY_LIMIT])
            raise PromptTooLongError(f'SSE error: {err_text}')
        _sse_err_type = eo.get('type', '') if isinstance(eo, dict) else ''
        _sse_http_code = str(eo.get('http_code', '')) if isinstance(eo, dict) else ''
        # Some upstream gateways (AWS Bedrock, GCP Vertex) embed the HTTP
        #   status inside the message text instead of a structured field,
        #   e.g. "(Service: BedrockRuntime, Status Code: 429, …)".
        if not _sse_http_code:
            _m = re.search(r'status code[:\s]+(\d{3})', _err_lower)
            if _m:
                _sse_http_code = _m.group(1)
        _sse_quota_patterns = [
            'too many tokens', 'too many requests',
            'quota exceeded', 'rate exceeded',
            'tokens per day', 'tokens per minute',
            'requests per day', 'requests per minute',
            'throttling', 'throttled',
        ]
        _sse_retryable_patterns = [
            '负载较高', 'server overload', 'service overload',
            'capacity', 'try again later', '稍后重试',
            'temporarily unavailable',
        ]
        _sse_non_retryable_patterns = [
            'not support model', 'invalid api key',
            'unauthorized', 'forbidden', 'not found',
            'plan not support', 'permission denied',
        ]
        _is_sse_non_retryable = any(p in _err_lower for p in _sse_non_retryable_patterns)
        _is_sse_quota = (
            not _is_sse_non_retryable
            and (
                _sse_http_code == '429'
                or any(p in _err_lower for p in _sse_quota_patterns)
            )
        )
        _is_sse_retryable = (
            not _is_sse_non_retryable
            and not _is_sse_quota
            and (
                _sse_err_type == 'server_error'
                or _sse_http_code.startswith('5')
                or any(p in _err_lower for p in _sse_retryable_patterns)
            )
        )
        if _is_sse_quota:
            logger.warning('%s SSE rate-limit/quota detected — escalating to '
                           'dispatch layer: %s', self.log_prefix, err_text[:_ERR_BODY_LIMIT])
            raise RateLimitError(
                f'SSE error: {err_text}',
                reason=f'HTTP 429: {err_text[:180]}')
        if _is_sse_retryable:
            _sse_status = int(_sse_http_code) if _sse_http_code.isdigit() else 500
            if _sse_status in _GATEWAY_THROTTLE_STATUS:
                logger.warning('%s SSE gateway throttle (HTTP %d) — escalating to '
                               'dispatch layer: %s', self.log_prefix, _sse_status,
                               err_text[:_ERR_BODY_LIMIT])
                raise RateLimitError(
                    f'SSE error: {err_text}', is_gateway=True,
                    reason=f'HTTP {_sse_status}: {err_text[:180]}')
            logger.warning('%s SSE server error (retryable): %s',
                           self.log_prefix, err_text[:_ERR_BODY_LIMIT])
            raise RetryableAPIError(
                f'SSE error: {err_text}',
                status_code=_sse_status)
        if not err_text:
            err_text = (f'<empty error body> sse_type={_sse_err_type or "?"} '
                        f'http_code={_sse_http_code or "?"} '
                        f'model={self.body.get("model", "?")} '
                        f'trace={self.trace_id}')
        raise Exception(f'SSE error: {err_text}')

    def _handle_delta(self, delta):
        """Accumulate thinking / content / tool-call deltas from one chunk."""
        # Thinking / reasoning delta
        td = (delta.get('thinking')
              or delta.get('reasoning_content')
              or (delta.get('content', '')
                  if delta.get('role') == 'thinking' else ''))
        # OpenRouter-style ``reasoning_details`` carry both the thinking text
        # and the opaque Claude signature, in separate chunks:
        #   [{"type":"thinking","thinking":"…"}]    ← text delta
        #   [{"type":"thinking","signature":"…"}]   ← signature (once per turn)
        # The Meituan/sankuai OpenAI-compat gateway uses exactly this shape
        # for Claude models, so harvest both keys here.
        rd_parts = delta.get('reasoning_details')
        if isinstance(rd_parts, list):
            if not td:
                td = ''.join(
                    (d.get('thinking') or d.get('text') or '')
                    for d in rd_parts if isinstance(d, dict))
            for d in rd_parts:
                if isinstance(d, dict) and d.get('signature'):
                    self.thinking_signature += d['signature']
        if td:
            self.thinking_text += td
            self._notify_reasoning_progress(td)
            if self.on_thinking:
                self.on_thinking(td)

        # Opaque thinking-block signature (Anthropic Messages API path).
        # Surfaced by the AnthropicSSETranslator as a synthetic delta field;
        # needed to replay the thinking block on a later tool-use turn.
        _tsig = delta.get('thinking_signature')
        if _tsig:
            self.thinking_signature += _tsig

        # Content delta
        if 'content' in delta and delta.get('role') != 'thinking':
            cd = delta['content'] or ''
            if cd:
                if self._mm_mode:
                    self._feed_minimax(cd)
                else:
                    self.content += cd
                    if self.progress.mark_content(cd):
                        self._notify_actionable_output()
                    if self.on_content:
                        self.on_content(cd)

        # Tool call deltas
        _tc_list = delta.get('tool_calls') or []
        if _tc_list:
            for tc in _tc_list:
                if not isinstance(tc, dict):
                    self.record_malformed_frames(
                        1, ('invalid_tool_delta: tool delta was not an object',))
                    continue
                _progress_fn = tc.get('function')
                if not isinstance(_progress_fn, dict):
                    _progress_fn = {}
                _progress_key = str(
                    tc.get('id') or f'index:{tc.get("index", "missing")}')
                # An id-only/empty function shell is protocol scaffolding, not
                # semantic progress. A non-blank function name opens the tool;
                # subsequent non-empty argument deltas renew the same clock.
                _recognized = bool(
                    str(_progress_fn.get('name') or '').strip())
                _new_recognized = bool(
                    _recognized and _progress_key not in self._progress_tool_slots)
                if _new_recognized:
                    self._progress_tool_slots.add(_progress_key)
                _argument_delta = _progress_fn.get('arguments')
                if not isinstance(_argument_delta, str):
                    _argument_delta = ''
                if self.progress.mark_tool_delta(
                        recognized=_new_recognized,
                        argument_delta=_argument_delta):
                    self._notify_actionable_output()
                # OBSERVATION: an absent ``index`` silently lands in slot 0,
                # which would merge two distinct tool calls. Never observed on
                # the bedrock line; unmeasured elsewhere. Log once per stream.
                if 'index' not in tc:
                    self._tc_obs_index_absent += 1
                    if self._tc_obs_index_absent == 1:
                        logger.warning(
                            '%s [tool_calls-shape] delta without "index" field '
                            '— defaulting to slot 0 (collision risk): '
                            'has_id=%s name=%r model=%s trace=%s',
                            self.log_prefix, bool(tc.get('id')),
                            (tc.get('function') or {}).get('name'),
                            self.body.get('model', ''), self.trace_id)
                        self.raw_dumper.dump_anomaly(
                            'tool_call_index_absent',
                            model=self.body.get('model', ''),
                            trace=self.trace_id,
                            chunks=self.chunk_count)
                # An absent ``index`` must NOT default to slot 0 — that merges
                # every unindexed call in the stream into one. Give it the next
                # free slot so distinct calls stay distinct.
                if 'index' in tc:
                    _upstream_idx = tc['index']
                    idx = self._tc_index_map.get(_upstream_idx, _upstream_idx)
                else:
                    _upstream_idx = None
                    idx = self._tc_unindexed_slot
                    if idx is None or self.tool_calls_acc.get(idx, {}).get(
                            'function', {}).get('name'):
                        # No open unindexed slot yet, or the current one is
                        # already named and this delta starts a new call.
                        if (tc.get('function') or {}).get('name') or idx is None:
                            idx = (max(self.tool_calls_acc) + 1
                                   if self.tool_calls_acc else 0)
                    self._tc_unindexed_slot = idx
                if idx not in self.tool_calls_acc:
                    if self.on_tool_call_ready and idx > 0 and (idx - 1) in self.tool_calls_acc:
                        _prev = self.tool_calls_acc[idx - 1]
                        try:
                            self.on_tool_call_ready(_prev)
                        except Exception as _tcr_err:
                            logger.debug('%s on_tool_call_ready callback error: %s',
                                         self.log_prefix, _tcr_err)
                    self.tool_calls_acc[idx] = {
                        'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                # Capture the slot's id BEFORE it is overwritten below — a
                # differing incoming id is how a second call in this slot is
                # told apart from an upstream re-issue of the same call, and
                # the overwrite would erase that evidence.
                _slot_id_before = self.tool_calls_acc[idx].get('id')
                if tc.get('id'):
                    self.tool_calls_acc[idx]['id'] = tc['id']
                if tc.get('extra_content'):
                    self.tool_calls_acc[idx]['extra_content'] = tc['extra_content']
                if isinstance(tc.get('caller'), dict):
                    self.tool_calls_acc[idx]['caller'] = dict(tc['caller'])
                fn = tc.get('function', {})
                if fn.get('name'):
                    _prev_name = self.tool_calls_acc[idx]['function']['name']
                    # A tool NAME is a one-shot identifier, not an incremental
                    # text field. Appending it (the pre-2026-07-27 behaviour)
                    # fused two calls that shared a slot into one
                    # undispatchable name like ``read_filesrun_command``.
                    #
                    # Three shapes arrive here and only the first is a genuine
                    # continuation:
                    #   * fragment of the CURRENT name (``read_`` → ``files``)
                    #     — the assembled string must stay one name;
                    #   * the SAME full name re-sent (non-incremental upstream)
                    #     — idempotent, keep one call;
                    #   * a DIFFERENT full name — a second call landed in this
                    #     slot; it gets its own slot instead of being appended.
                    _incoming = fn['name']
                    _new_id = bool(tc.get('id')) and bool(_slot_id_before) \
                        and tc['id'] != _slot_id_before
                    if not _prev_name:
                        self.tool_calls_acc[idx]['function']['name'] = _incoming
                    elif _prev_name == _incoming and not _new_id:
                        # Upstream re-issued the whole name for the SAME call
                        # (non-incremental semantics) — idempotent.
                        pass
                    elif not _new_id and self._tc_is_name_fragment(
                            _prev_name, _incoming, tc, idx):
                        self.tool_calls_acc[idx]['function']['name'] = _prev_name + _incoming
                    else:
                        self._tc_obs_slot_split += 1
                        logger.warning(
                            '%s [tool_calls-shape] second tool name in slot %s '
                            '— opening a NEW slot instead of concatenating: '
                            'prev=%r incoming=%r incoming_id=%r model=%s '
                            'trace=%s chunk=%d',
                            self.log_prefix, idx, _prev_name, _incoming,
                            tc.get('id'), self.body.get('model', ''),
                            self.trace_id, self.chunk_count)
                        self.raw_dumper.dump_anomaly(
                            'tool_call_slot_split',
                            model=self.body.get('model', ''),
                            trace=self.trace_id,
                            slot=idx, prev_name=_prev_name,
                            incoming_name=_incoming,
                            chunks=self.chunk_count)
                        # The overwrite above put the NEW call's id on the old
                        # slot; give it back its own id before moving on.
                        if _slot_id_before:
                            self.tool_calls_acc[idx]['id'] = _slot_id_before
                        idx = max(self.tool_calls_acc) + 1
                        self._tc_unindexed_slot = idx
                        if _upstream_idx is not None:
                            # Re-point this upstream index at the new slot so
                            # the call's own argument deltas follow it.
                            self._tc_index_map[_upstream_idx] = idx
                        self.tool_calls_acc[idx] = {
                            'id': tc.get('id') or '', 'type': 'function',
                            'function': {'name': _incoming, 'arguments': ''},
                        }
                        if tc.get('extra_content'):
                            self.tool_calls_acc[idx]['extra_content'] = tc['extra_content']
                        if isinstance(tc.get('caller'), dict):
                            self.tool_calls_acc[idx]['caller'] = dict(tc['caller'])
                if fn.get('arguments') is not None:
                    self.tool_calls_acc[idx]['function']['arguments'] += fn.get('arguments', '')

    def _tc_is_name_fragment(self, prev_name, incoming, tc, idx):
        """Is ``incoming`` the CONTINUATION of ``prev_name``, not a new call?

        Distinguishing a streamed name fragment from a second call that landed
        in the same slot decides whether the two strings may be joined. The
        dispatched toolset is the oracle: joining is right only when the joined
        string is a real tool name (or a prefix of one) while ``prev_name``
        alone is not yet complete. A new ``id`` arriving with the delta always
        means a new call, whatever the strings look like.
        """
        _slot = self.tool_calls_acc.get(idx) or {}
        _incoming_id = tc.get('id')
        if _incoming_id and _slot.get('id') and _incoming_id != _slot['id']:
            return False
        if _slot.get('function', {}).get('arguments'):
            # Arguments already started for this call — a name arriving now
            # cannot be part of its identifier.
            return False
        try:
            _sent = _wire_tool_names(self.body.get('tools') or [])
        except Exception as _e:
            logger.debug('%s toolset read failed in fragment check: %s',
                         self.log_prefix, _e)
            _sent = set()
        if not _sent:
            # No oracle available. Arguments-not-started was already checked
            # above, which is the only safe structural cue we have; treat the
            # delta as a fragment so a genuinely chunked name still assembles.
            return True
        joined = prev_name + incoming
        prev_complete = prev_name in _sent
        joined_plausible = joined in _sent or any(
            n.startswith(joined) for n in _sent)
        return joined_plausible and not prev_complete

    def _feed_minimax(self, cd):
        """MiniMax inline ``<think>…</think>`` demux into thinking vs content."""
        self._mm_buf += cd
        while self._mm_buf:
            if self._mm_in_think:
                end_idx = self._mm_buf.find('</think>')
                if end_idx == -1:
                    self.thinking_text += self._mm_buf
                    self._notify_reasoning_progress(self._mm_buf)
                    if self.on_thinking:
                        self.on_thinking(self._mm_buf)
                    self._mm_buf = ''
                else:
                    think_part = self._mm_buf[:end_idx]
                    if think_part:
                        self.thinking_text += think_part
                        self._notify_reasoning_progress(think_part)
                        if self.on_thinking:
                            self.on_thinking(think_part)
                    self._mm_buf = self._mm_buf[end_idx + len('</think>'):]
                    self._mm_in_think = False
            else:
                start_idx = self._mm_buf.find('<think>')
                if start_idx == -1:
                    if len(self._mm_buf) > 7 and '<' in self._mm_buf[-7:]:
                        safe = self._mm_buf[:self._mm_buf.rfind('<', max(0, len(self._mm_buf) - 7))]
                        if safe:
                            self.content += safe
                            if self.progress.mark_content(safe):
                                self._notify_actionable_output()
                            if self.on_content:
                                self.on_content(safe)
                        self._mm_buf = self._mm_buf[len(safe):]
                    else:
                        self.content += self._mm_buf
                        if self.progress.mark_content(self._mm_buf):
                            self._notify_actionable_output()
                        if self.on_content:
                            self.on_content(self._mm_buf)
                        self._mm_buf = ''
                else:
                    before = self._mm_buf[:start_idx]
                    if before:
                        self.content += before
                        if self.progress.mark_content(before):
                            self._notify_actionable_output()
                        if self.on_content:
                            self.on_content(before)
                    self._mm_buf = self._mm_buf[start_idx + len('<think>'):]
                    self._mm_in_think = True

    def fire_final_tool_callback(self):
        """Fire on_tool_call_ready for the LAST accumulated tool call."""
        if self.on_tool_call_ready and self.tool_calls_acc:
            _last_idx = max(self.tool_calls_acc.keys())
            _last_tc = self.tool_calls_acc[_last_idx]
            if _last_tc['function']['name']:
                try:
                    self.on_tool_call_ready(_last_tc)
                except Exception as _tcr_err:
                    logger.debug('%s on_tool_call_ready callback error (final): %s',
                                 self.log_prefix, _tcr_err)

    def finalize(self, *, resp_trace=''):
        """Flush buffers, build the assistant msg, emit diagnostics + usage.

        Returns a typed, tuple-compatible ``ProviderStreamResult`` with the
        former message/finish/usage projection and one closed semantic state.
        """
        # Flush MiniMax buffer
        if self._mm_mode and self._mm_buf:
            if self._mm_in_think:
                self.thinking_text += self._mm_buf
                self._notify_reasoning_progress(self._mm_buf)
                if self.on_thinking:
                    self.on_thinking(self._mm_buf)
            else:
                self.content += self._mm_buf
                if self.progress.mark_content(self._mm_buf):
                    self._notify_actionable_output()
                if self.on_content:
                    self.on_content(self._mm_buf)
            self._mm_buf = ''

        # MiniMax: normalize reasoning_tokens into usage
        if self._mm_mode and self.usage and self.thinking_text:
            ctd = self.usage.get('completion_tokens_details', {})
            rt = ctd.get('reasoning_tokens', 0)
            if rt > 0 and 'reasoning_tokens' not in self.usage:
                self.usage['reasoning_tokens'] = rt

        # Pre-filter count — the tool_calls_no_payload anomaly check below
        # uses this to tell "the GATEWAY never sent any tool_call delta" (0)
        # apart from "OUR phantom filter dropped every accumulated entry"
        # (>0 — its per-entry WARNINGs then exist in the log).
        _tool_calls_seen = len(self.tool_calls_acc)

        # Filter out spurious tool calls
        if self.tool_calls_acc:
            _filtered = {}
            _names_with_args = {
                tc['function']['name']
                for tc in self.tool_calls_acc.values()
                if (tc['function'].get('arguments', '') or '').strip()
            }
            for idx, tc_entry in self.tool_calls_acc.items():
                fn_name = tc_entry['function']['name']
                fn_args_str = tc_entry['function'].get('arguments', '')
                if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
                    logger.debug('%s Filtering spurious internal tool call: %s',
                                 self.log_prefix, fn_name)
                    continue
                if not fn_args_str.strip() and fn_name in _names_with_args:
                    logger.warning(
                        '%s Filtering phantom tool call: %s (tc_id=%s) has '
                        'empty arguments — duplicate of another %s call with real args',
                        self.log_prefix, fn_name, tc_entry.get('id', '?')[:12], fn_name,
                    )
                    continue
                # ── Normalize empty/whitespace arguments to '{}' ──
                # A genuine no-arg tool call (or one whose args delta never
                # arrived) survives the phantom filter above with arguments=''.
                # OpenAI/Anthropic tolerate that (the executor does
                # ``json.loads(args or '{}')``), but Gemini's OpenAI-compat
                # proxy REJECTS a replayed assistant tool_call with empty
                # arguments — HTTP 400 "Expected function 'arguments' in a(n)
                # 'assistant' message to be populated." — killing the whole
                # follow-up turn. We emit '{}' (valid empty JSON object) so the
                # message replays cleanly across every provider. Equivalent to
                # the empty→'{}' coercion the DB-history replay builders already
                # apply (conv_message_builder / message_builder).
                if not fn_args_str.strip():
                    tc_entry['function']['arguments'] = '{}'
                _filtered[idx] = tc_entry
            self.tool_calls_acc = _filtered

        # ── OBSERVATION 3: final tool name not in the dispatched toolset ──
        # The two per-delta observations above only fire when a name arrives
        # into an already-named slot. A model that emits an already-concatenated
        # name in its FIRST frame trips NEITHER of them — so their silence is
        # indistinguishable from "this code never ran". That makes silence
        # useless as evidence. This check gives that case a POSITIVE signature:
        # compare the finished name against the tool whitelist we actually sent
        # upstream, and report it together with both counters.
        #
        #   name_reissue>0 & identical=False → two distinct calls, one slot (our bug)
        #   name_reissue>0 & identical=True  → upstream re-issued the name (our bug)
        #   unknown_name  & both counters 0  → model emitted it whole (model side)
        if self.tool_calls_acc:
            try:
                # Two dispatched-tool shapes exist on the wire and BOTH must be
                # understood here, or this check silently no-ops on one line —
                # the exact failure mode it was written to eliminate:
                #   OpenAI-compat : {'type':'function','function':{'name':…}}
                #   Anthropic     : {'name':…,'description':…,'input_schema':…}
                # (see lib/llm/anthropic_outbound/_to_anthropic.py). Reading
                # only the nested form yields an EMPTY whitelist on the
                # Anthropic line, which would skip the loop entirely.
                _sent_names = _wire_tool_names(self.body.get('tools') or [])
                if _sent_names:
                    for _idx, _tc in self.tool_calls_acc.items():
                        _nm = _tc['function']['name']
                        if not _nm or _nm in _sent_names:
                            continue
                        _args = _tc['function'].get('arguments', '') or ''
                        try:
                            json.loads(_args or '{}')
                            _args_valid = True
                        except Exception as _e:
                            logger.debug('finalize: failed (%s)', _e)
                            _args_valid = False
                        logger.warning(
                            '%s [tool_calls-shape] final tool name NOT in '
                            'dispatched toolset: name=%r slot=%s tc_id=%r '
                            'slots=%d args_valid_json=%s args_len=%d '
                            'obs_name_reissue=%d obs_index_absent=%d '
                            'tools_sent=%d model=%s trace=%s',
                            self.log_prefix, _nm, _idx,
                            _tc.get('id'), len(self.tool_calls_acc),
                            _args_valid, len(_args),
                            self._tc_obs_name_reissue,
                            self._tc_obs_index_absent,
                            len(_sent_names),
                            self.body.get('model', ''), self.trace_id)
                        self.raw_dumper.dump_anomaly(
                            'tool_name_unknown',
                            model=self.body.get('model', ''),
                            trace=self.trace_id,
                            name=_nm,
                            slot=_idx,
                            args_valid_json=_args_valid,
                            obs_name_reissue=self._tc_obs_name_reissue,
                            obs_index_absent=self._tc_obs_index_absent,
                        )
                else:
                    # The model returned tool calls but we could not build a
                    # whitelist — so this check CANNOT vet the names, and its
                    # silence would otherwise read as "names were fine". Say so
                    # out loud, or the observation acquires a third
                    # indistinguishable-silence mode of its own.
                    logger.warning(
                        '%s [tool_calls-shape] %d tool call(s) returned but the '
                        'dispatched toolset could not be resolved (tools field '
                        'empty/unrecognized shape) — name check SKIPPED, not '
                        'passed: names=%r tools_raw_len=%d model=%s trace=%s',
                        self.log_prefix, len(self.tool_calls_acc),
                        [t['function']['name']
                         for t in self.tool_calls_acc.values()],
                        len(self.body.get('tools') or []),
                        self.body.get('model', ''), self.trace_id)
            except Exception as _unk_err:
                logger.debug('%s tool-name whitelist check failed: %s',
                             self.log_prefix, _unk_err)

        content = self.content
        thinking_text = self.thinking_text
        tool_calls_acc = self.tool_calls_acc
        provider_finish_reason = self.finish_reason
        # Tuple compatibility only.  All semantic decisions below use
        # ``provider_finish_reason`` / ``saw_finish_reason`` instead.
        finish_reason = provider_finish_reason or 'stop'
        usage = self.usage

        # Build assistant message
        msg = {'role': 'assistant'}
        _responses_items = getattr(
            self.wire_translator, 'response_items', None)
        if _responses_items:
            # Private canonical sidecar: protocol converters replay it, while
            # Chat-Completions/Anthropic allowlists never put it on their wire.
            msg['_responses_items'] = [dict(item)
                                       for item in _responses_items
                                       if isinstance(item, dict)]
        _anthropic_blocks = getattr(
            self.wire_translator, 'anthropic_content_blocks', None)
        if _anthropic_blocks:
            # Same protocol-private replay sidecar pattern as Responses output
            # items.  It never reaches an OpenAI-compatible message wire.
            msg['_anthropic_content_blocks'] = [dict(block)
                                                for block in _anthropic_blocks
                                                if isinstance(block, dict)]
        if thinking_text:
            msg['reasoning_content'] = thinking_text
        if self.thinking_signature:
            msg['thinking_signature'] = self.thinking_signature
        if tool_calls_acc:
            msg['tool_calls'] = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            if content:
                msg['content'] = content
        else:
            msg['content'] = content

        # Log cache info. Stamp the canonical cache keys onto the raw usage
        # dict FIRST so every downstream raw-dict consumer (api_rounds, the
        # SSE usage payload, persisted metadata, the frontend popover) sees
        # cache hits regardless of the provider's spelling (kimi reports hits
        # as cached_tokens while pinning cache_read_tokens=0). The log line
        # itself reads via normalize_usage so unstamped paths stay covered.
        cache_info = ''
        if usage:
            canonicalize_usage_cache_keys(usage)
            _nu = normalize_usage(usage)
            cw = _nu['cache_write']
            cr = _nu['cache_read']
            if cw or cr:
                cache_info = f' cache_w={cw} cache_r={cr}'
                if cr > 0:
                    inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
                    cache_info += f' (saved ~{round(cr / max(inp, 1) * 100)}%)'

        if self.log_prefix:
            logger.debug('%s Done: finish=%s content=%d think=%d%s', self.log_prefix,
                         finish_reason, len(content), len(thinking_text), cache_info)

        _stream_evidence = self.progress.evidence()
        _stream_elapsed_s = _stream_evidence.request_elapsed_ms / 1000
        aborted = self.aborted_by_client
        chunk_count = self.chunk_count

        # Diagnostics: detect premature stream close
        if not aborted and not self.saw_done:
            logger.warning(
                '%s ⚠ PREMATURE STREAM CLOSE: Server never sent [DONE] marker. '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs chunks_received=%d '
                'saw_finish_reason=%s finish_reason=%s content_len=%d thinking_len=%d '
                'tool_calls=%d model=%s url=%s',
                self.log_prefix, self.trace_id, resp_trace or 'none',
                _stream_elapsed_s, chunk_count,
                self.saw_finish_reason, finish_reason,
                len(content), len(thinking_text),
                len(tool_calls_acc), self.body.get('model', '?'), self.url)
            if self.semantic_progress_timeout_s > 0:
                _stall_diag = self.semantic_progress_diagnostics
                _request_elapsed = _stall_diag.get(
                    'request_elapsed_s', _stream_elapsed_s)
                _last_progress_age = _stall_diag.get(
                    'last_progress_age_s',
                    self.semantic_progress_timeout_s)
                _reasoning_chunks = int(_stall_diag.get(
                    'reasoning_chunks', 0))
                logger.warning(
                    '%s NO SEMANTIC PROGRESS: no new reasoning, assistant '
                    'content, or tool action for %.1fs (window=%.1fs); '
                    'request_elapsed=%.1fs reasoning=%d chars/%d semantic '
                    'chunks sse_chunks=%d model=%s recovery policy will decide.',
                    self.log_prefix, _last_progress_age,
                    self.semantic_progress_timeout_s, _request_elapsed,
                    len(thinking_text), _reasoning_chunks, chunk_count,
                    self.body.get('model', '?'))
                self.raw_dumper.dump_anomaly(
                    'semantic_progress_timeout',
                    timeout_s=round(
                        self.semantic_progress_timeout_s, 2),
                    elapsed_s=round(_stream_elapsed_s, 2),
                    request_elapsed_s=round(_request_elapsed, 2),
                    last_progress_age_s=round(_last_progress_age, 2),
                    chunks=chunk_count,
                    reasoning_chunks=_reasoning_chunks,
                    saw_finish_reason=self.saw_finish_reason,
                    finish_reason=finish_reason,
                    content_len=len(content),
                    thinking_len=len(thinking_text),
                    tool_calls=len(tool_calls_acc),
                    resp_trace=resp_trace or 'none',
                    model=self.body.get('model', ''),
                )
            else:
                self.raw_dumper.dump_anomaly(
                    'missing_done',
                    elapsed_s=round(_stream_elapsed_s, 2),
                    chunks=chunk_count,
                    saw_finish_reason=self.saw_finish_reason,
                    finish_reason=finish_reason,
                    content_len=len(content),
                    thinking_len=len(thinking_text),
                    tool_calls=len(tool_calls_acc),
                    resp_trace=resp_trace or 'none',
                )
        elif not aborted and not self.saw_finish_reason:
            logger.warning(
                '%s ⚠ MISSING FINISH_REASON: stream closed without an '
                'authoritative finish_reason (saw_done=%s). M-TraceId=%s '
                'elapsed=%.1fs compatibility_default=%s chunks=%d '
                'content_len=%d model=%s',
                self.log_prefix, self.saw_done, self.trace_id, _stream_elapsed_s,
                finish_reason, chunk_count, len(content), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'missing_finish_reason',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                content_len=len(content),
                thinking_len=len(thinking_text),
                tool_calls=len(tool_calls_acc),
            )

        # Diagnostics: detect empty responses
        _empty_response = bool(
            not aborted
            and self.saw_finish_reason
            and provider_finish_reason == 'stop'
            and not content
            and not tool_calls_acc
        )
        if _empty_response:
            logger.warning(
                '%s ⚠ EMPTY STOP RESPONSE: finish=stop but no content and no tool_calls. '
                'M-TraceId=%s elapsed=%.1fs chunks=%d thinking_len=%d model=%s',
                self.log_prefix, self.trace_id, _stream_elapsed_s,
                chunk_count, len(thinking_text), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'empty_stop',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                thinking_len=len(thinking_text),
                finish_reason=finish_reason,
                resp_trace=resp_trace or 'none',
            )

        # Diagnostics: tool_calls finish reason with ZERO payload
        # (2026-08-06 kimi-k3/sankuai incident, conv msh3qeplzneph5 R3).
        # The gateway closed the stream cleanly reporting
        # finish_reason=tool_calls, yet not one tool_call delta accumulated
        # — the model's tool calls were lost UPSTREAM. Downstream this used
        # to normalize to a fake "stop" and end the turn mid-work with a
        # preamble as the deliverable. None of the four anomaly classes
        # above covers this shape, so dump the raw frames here and stamp
        # usage for the loop classifier (its retry bucket reads
        # ``_tool_calls_void``). ``cause`` separates the two worlds:
        #   'gateway_no_payload' — pre-filter count 0: the wire itself
        #     carried no tool_call deltas (upstream loss);
        #   'filtered' — pre-filter count >0: OUR phantom filter dropped
        #     every entry (its per-entry WARNINGs are then in the log).
        _tool_calls_void = None
        if (not aborted and provider_finish_reason in ('tool_calls', 'tool_use')
                and not tool_calls_acc and chunk_count > 0):
            _tool_calls_void = ('filtered' if _tool_calls_seen
                                else 'gateway_no_payload')
            logger.warning(
                '%s ⚠ TOOL_CALLS FINISH WITHOUT PAYLOAD: finish_reason=%s '
                'but 0 tool call(s) assembled (cause=%s pre_filter=%d). '
                'M-TraceId=%s elapsed=%.1fs chunks=%d content_len=%d '
                'thinking_len=%d model=%s',
                self.log_prefix, finish_reason, _tool_calls_void,
                _tool_calls_seen, self.trace_id, _stream_elapsed_s,
                chunk_count, len(content), len(thinking_text),
                self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'tool_calls_no_payload',
                cause=_tool_calls_void,
                pre_filter_count=_tool_calls_seen,
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                content_len=len(content),
                thinking_len=len(thinking_text),
                finish_reason=finish_reason,
                mm_mode=self._mm_mode,
                resp_trace=resp_trace or 'none',
            )

        if not aborted and self._malformed_frame_count:
            logger.warning(
                '%s ⚠ MALFORMED STREAM: %d provider frame(s) could not be '
                'decoded; the response is incomplete even if a later finish '
                'frame arrived. M-TraceId=%s model=%s',
                self.log_prefix, self._malformed_frame_count, self.trace_id,
                self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'malformed_stream',
                malformed_frames=self._malformed_frame_count,
                chunks=chunk_count,
                saw_done=self.saw_done,
                saw_finish_reason=self.saw_finish_reason,
                finish_reason=provider_finish_reason,
                model=self.body.get('model', ''),
            )

        stream_state = classify_provider_stream_state(
            aborted=aborted,
            saw_finish_reason=self.saw_finish_reason,
            malformed_frame_count=self._malformed_frame_count,
            empty_response=_empty_response,
            tool_payload_missing=bool(_tool_calls_void),
            semantic_progress_timeout=(
                self.semantic_progress_timeout_s > 0),
        )
        _stream_evidence = replace(
            _stream_evidence,
            empty_response=_empty_response,
            tool_payload_missing=bool(_tool_calls_void),
            tool_payload_missing_cause=_tool_calls_void or '',
        )

        # Inject metadata into usage
        if usage is None:
            usage = {}
        usage['trace_id'] = self.trace_id
        if resp_trace and resp_trace != self.trace_id:
            usage['resp_trace_id'] = resp_trace
        # ── Relay the post-translation wire fingerprint into `usage` ──
        # Captured in prepare_request (the only point seeing the final wire
        # bytes) and carried on the RawSSEDumper. detect_cache_break reads these
        # to distinguish a PROVEN server-side miss (fingerprint identical to
        # last round) from a client-caused one (names the changed msg.field).
        _wfp = getattr(self.raw_dumper, 'wire_fp', None)
        if _wfp is not None:
            usage['_wire_fp'] = _wfp
            usage['_wire_static'] = getattr(self.raw_dumper, 'wire_static', '')
            _wsys = getattr(self.raw_dumper, 'wire_system', None)
            if _wsys is not None:
                usage['_wire_system'] = _wsys
            _wmk = getattr(self.raw_dumper, 'wire_markers', None)
            if _wmk is not None:
                usage['_wire_markers'] = _wmk
            _wbytes = getattr(self.raw_dumper, 'wire_bytes', None)
            if _wbytes is not None:
                usage['_wire_bytes'] = _wbytes
            _wfbytes = getattr(self.raw_dumper, 'wire_field_bytes', None)
            if _wfbytes is not None:
                usage['_wire_field_bytes'] = _wfbytes
            _wregion = getattr(self.raw_dumper, 'wire_region', None)
            if _wregion is not None:
                usage['_wire_region'] = _wregion

        # Cache-namespace routing fingerprint — relayed on its OWN guard (not
        # nested under _wfp), because routing is captured independently of the
        # body fingerprint in prepare_request, so it can be present even when
        # the message-fingerprint capture failed. detect_cache_break diffs it to
        # name a client cache-namespace switch (key/beta/endpoint flip) instead
        # of laundering a byte-identical miss into "server-side".
        _wrouting = getattr(self.raw_dumper, 'wire_routing', None)
        if _wrouting is not None:
            usage['_wire_routing'] = _wrouting

        self.raw_dumper.finish(
            finish_reason=finish_reason,
            content_len=len(content),
            thinking_len=len(thinking_text),
            tool_calls=len(tool_calls_acc),
            saw_done=self.saw_done,
            saw_finish_reason=self.saw_finish_reason,
        )

        result = ProviderStreamResult(
            message=msg,
            compatibility_finish_reason=finish_reason,
            usage=usage,
            state=stream_state,
            provider_finish_reason=provider_finish_reason,
            saw_done=self.saw_done,
            saw_finish_reason=self.saw_finish_reason,
            malformed_frame_count=self._malformed_frame_count,
            evidence=_stream_evidence,
        )
        return result.with_usage(usage)
