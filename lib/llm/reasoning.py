"""Compile immutable JSON reasoning intent to one destination's wire contract.

Entry points: capture_intent (product ingress), compile_reasoning (initial/retry).
Only the no-profile compatibility boundary may consult model names.
"""
import copy

import math

from lib.llm_errors import LocalRequestPreparationError

SAMPLING = ('temperature', 'top_p', 'top_k', 'presence_penalty',
            'frequency_penalty', 'stop')
WIRE = ('thinking', 'enable_thinking', 'reasoning_split', 'reasoning_effort', 'effort')


class ReasoningContractError(LocalRequestPreparationError, ValueError):
    """Bounded pre-provider incompatibility; never contains caller content."""
    def __init__(self, kind, details=''):
        self.kind = str(kind)[:64]
        self.details = str(details)[:160]
        super().__init__(f'{self.kind}: {self.details}')


def capture_intent(*, thinking_enabled=None, thinking_depth=None, effort=None,
                   preset='medium', temperature=1.0, extra=None):
    extra = extra or {}
    mode, value, strict = 'default', None, False

    for choice in (thinking_depth, effort, preset):
        if choice is not None and (not isinstance(choice, str) or len(choice) > 64):
            raise ReasoningContractError('invalid_intent', 'reasoning choice is invalid')
    if thinking_enabled is not None and not isinstance(thinking_enabled, bool):
        raise ReasoningContractError('invalid_intent', 'reasoning enablement must be boolean')
    if thinking_enabled is False and thinking_depth not in ('off', 'default', None):
        mode, value, strict = 'off', None, True
    elif thinking_depth is not None:
        mode = thinking_depth if thinking_depth in ('off', 'default', 'lowest', 'enabled') else 'preset'
        value, strict = thinking_depth, True
    elif effort is not None:
        mode = 'off' if effort == 'none' else effort if effort in ('off', 'default', 'lowest', 'enabled') else 'exact'
        value, strict = effort, True
    elif thinking_enabled is not None:
        mode, value = ('preset', preset) if thinking_enabled else ('off', None)
    elif any(key in extra for key in WIRE) or any(
            key in extra for key in ('chat_template_kwargs', 'reasoning', 'output_config')):
        return native_intent(dict(extra, temperature=extra.get('temperature', temperature)))
    sampling = {'temperature': temperature}
    sampling.update({key: copy.deepcopy(extra[key]) for key in SAMPLING if key in extra})
    return {'version': 1, 'mode': mode, 'value': value, 'strict': strict,
            'sampling': sampling, 'source': 'product'}


def native_reasoning_effort(body):
    """Read all native spellings once; no alias silently shadows another."""
    values = [body[key] for key in ('effort', 'reasoning_effort') if body.get(key) is not None]
    for key in ('reasoning', 'output_config'):
        section = body.get(key)
        if isinstance(section, dict) and section.get('effort') is not None:
            values.append(section['effort'])
    if any(not isinstance(value, str) or not value or len(value) > 64 for value in values):
        raise ReasoningContractError('invalid_intent', 'reasoning effort is invalid')
    if values and any(value != values[0] for value in values[1:]):
        raise ReasoningContractError('reasoning_effort_conflict', 'native efforts disagree')
    return values[0] if values else None


def native_intent(body):
    """One-time compatibility ingress; none is off, minimal is exact effort."""
    sections = {}
    for key in ('thinking', 'chat_template_kwargs', 'reasoning', 'output_config'):
        section = body.get(key, {})
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise ReasoningContractError('invalid_intent', 'native reasoning fields must be objects')
        sections[key] = section
    effort = native_reasoning_effort(body)
    thinking = sections['thinking']
    template = sections['chat_template_kwargs']
    enabled = body.get('enable_thinking', template.get('enable_thinking'))
    if thinking.get('type') in ('enabled', 'adaptive', 'disabled'):
        enabled = thinking['type'] != 'disabled'
    if effort == 'none' or enabled is False:
        mode, value = 'off', None
    elif effort is not None:
        mode, value = 'exact', effort
    elif enabled is True or body.get('reasoning_split'):
        mode, value = 'enabled', None
    else:
        mode, value = 'default', None
    budget = thinking.get('budget_tokens')
    if budget is not None and (type(budget) is not int or not 1 <= budget <= 1000000):
        raise ReasoningContractError('invalid_intent', 'reasoning budget is invalid')
    if budget is not None and mode == 'default':
        mode = 'enabled'
    return {'version': 1, 'mode': mode, 'value': value, 'strict': mode != 'default',
            'sampling': {k: copy.deepcopy(body[k]) for k in SAMPLING if k in body},
            'source': 'native', **({'budget_tokens': budget} if budget is not None else {})}


def _validate_intent(intent):
    """Only bounded generation inputs may be restored; never routing/body fields."""
    if (not isinstance(intent, dict) or type(intent.get('version')) is not int
            or intent['version'] != 1
            or not isinstance(intent.get('mode'), str)
            or intent['mode'] not in {'default', 'off', 'lowest', 'enabled', 'preset', 'exact'}
            or not isinstance(intent.get('sampling', {}), dict)
            or not isinstance(intent.get('strict'), bool)):
        raise ReasoningContractError('invalid_intent', 'reasoning intent is invalid')
    if set(intent) - {'version', 'mode', 'value', 'strict', 'sampling', 'source', 'budget_tokens'}:
        raise ReasoningContractError('invalid_intent', 'unknown reasoning intent fields')
    value = intent.get('value')
    if value is not None and (not isinstance(value, str) or not value or len(value) > 64):
        raise ReasoningContractError('invalid_intent', 'reasoning effort is invalid')
    if intent['mode'] in {'exact', 'preset'} and value is None:
        raise ReasoningContractError('invalid_intent', 'reasoning effort is required')
    if not isinstance(intent.get('source', 'native'), str) or intent.get('source', 'native') not in ('native', 'product'):
        raise ReasoningContractError('invalid_intent', 'reasoning source is invalid')
    sampling = intent.get('sampling', {})
    if set(sampling) - set(SAMPLING):
        raise ReasoningContractError('invalid_intent', 'unknown sampling fields')
    for key, value in sampling.items():
        if value is None:
            continue
        if key == 'stop':
            values = [value] if isinstance(value, str) else value
            if (not isinstance(values, list) or len(values) > 64
                    or any(not isinstance(v, str) or len(v) > 8192 for v in values)):
                raise ReasoningContractError('invalid_intent', 'stop sequences are invalid')
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ReasoningContractError('invalid_intent', 'sampling values must be finite numbers')
    budget = intent.get('budget_tokens')
    if budget is not None and (type(budget) is not int or not 1 <= budget <= 1000000):
        raise ReasoningContractError('invalid_intent', 'reasoning budget is invalid')


def _clear(body):
    for key in WIRE + SAMPLING:
        body.pop(key, None)

    # Nested native effort is ingress only, not a second authority on retarget.
    for key in ('reasoning', 'output_config'):
        if isinstance(body.get(key), dict) and 'effort' in body[key]:
            section = dict(body[key])
            section.pop('effort')
            if section:
                body[key] = section
            else:
                body.pop(key)
    kwargs = body.get('chat_template_kwargs')
    if isinstance(kwargs, dict):
        kwargs = copy.deepcopy(kwargs)
        for key in ('enable_thinking', 'thinking', 'reasoning_effort', 'effort'):
            kwargs.pop(key, None)
        if kwargs:
            body['chat_template_kwargs'] = kwargs
        else:
            body.pop('chat_template_kwargs', None)


def _canonical_ordering(efforts, reasoning):
    """Best-effort cheapest→richest ordering of a profile's selectable rungs.

    Preserves the profile's declared order; only fills gaps for rungs whose
    canonical rank is known. Unknown/future native names keep their position,
    so clamping never invents a rung the destination did not declare.
    """
    disabled = reasoning.get('disabled_effort')
    selectable = [e for e in efforts if e not in ('none', disabled)]
    rank = {name: i for i, name in enumerate(_RANKED_DEPTHS)}
    known = sorted([e for e in selectable if e in rank], key=rank.__getitem__)
    unknown = [e for e in selectable if e not in rank]
    # Unknown rungs are spliced in their declared position relative to known
    # ones, so a future ladder ordering is preserved end to end.
    out, ki, ui = [], 0, 0
    for e in selectable:
        if e in rank:
            out.append(known[ki]); ki += 1
        else:
            out.append(unknown[ui]); ui += 1
    return out


def _cheapest_rung(efforts, reasoning):
    """The lightest enabled rung; None when the profile has no effort ladder."""
    order = _canonical_ordering(efforts, reasoning)
    return order[0] if order else None


def _clamp_preset(value, efforts, reasoning):
    """Map a preset onto the destination ladder, clamping down to the nearest
    representable rung at-or-below the request, else the destination default.

    Returns (resolved_effort_or_None, clamped?) — never invents a rung.
    """
    effort_map = reasoning.get('effort_map', {})
    mapped = value if value in efforts else effort_map.get(value, value)
    if mapped in efforts:
        return mapped, mapped != value
    order = _canonical_ordering(efforts, reasoning)
    rank = {name: i for i, name in enumerate(_RANKED_DEPTHS)}
    if value in rank and order:
        below = [e for e in order if e in rank and rank[e] <= rank[value]]
        if below:
            return below[-1], True
    default = reasoning.get('default_effort')
    if default in efforts:
        return default, True
    return (order[0] if order else None), True


def resolve_reasoning_control(reasoning, mode, value=None, *, strict=True):
    """Resolve a declared control without executing plugins or reading model names.

    Shared by request compilation and composer projection: a displayed choice
    must have an installed, representable wire binding, not just a support label.
    Returns (wire_format, enabled, resolved_effort, mode). Default emits no
    control, EXCEPT on the ``reasoning_effort`` wire with a declared
    ``default_effort`` — see the mode == 'default' branch below.

    Degrade-and-record is the product contract: a ``preset``/``enabled``/
    ``lowest``/``off`` intent that the destination cannot represent exactly
    degrades to the nearest representable rung (recorded by the caller in
    ``_reasoning_resolution``) instead of failing the whole turn. An ``off``
    the destination cannot honor becomes the cheapest enabled rung, or — when
    the model has no effort ladder at all — a bare enabled control, so a
    cannot-disable model still answers rather than 500s.

    Only an ``exact`` effort may fail closed: it is a precise API guarantee
    (native replay / programmatic callers), so an unrepresentable exact effort
    raises ``unsupported_effort`` rather than silently substitute a different
    effort.
    """
    fmt = reasoning.get('wire_format', 'none')
    support = reasoning.get('support', 'unknown')
    enabled = mode not in ('off', 'default')
    if mode == 'default':
        # "Default" follows the model's DECLARED default rung. On the
        # reasoning_effort wire that rung is the only reasoning control, so
        # it is emitted explicitly: a destination that honors its documented
        # default treats the explicit rung as a no-op, while a destination
        # whose implicit default diverges is repaired (kimi-k3 pinned to the
        # Anthropic face, 2026-09-10: no control streams little/no thinking
        # although the declared default is max). Other wire formats keep
        # omission semantics — Claude's adaptive default must not become an
        # explicit rung, and an unknown/unsupported destination stays
        # fail-soft.
        if support == 'supported' and fmt == 'reasoning_effort':
            default = reasoning.get('default_effort')
            if default is not None:
                return fmt, True, default, 'default'
        return fmt, False, None, 'default'
    if support != 'supported':
        if mode == 'off' and support == 'unsupported':
            return 'none', False, None, 'off'
        raise ReasoningContractError('unsupported_reasoning', 'destination reasoning support is not declared')
    cannot_disable = reasoning.get('can_disable') is not True
    if mode == 'off' and cannot_disable:
        # Degrade a request to disable reasoning into the smallest reasoning
        # the model will actually do, instead of failing the whole turn.
        mode = 'lowest'
        enabled = True
    if fmt == 'none':
        raise ReasoningContractError('unrepresentable_reasoning', 'destination has no reasoning control')
    if fmt not in ('thinking_type', 'reasoning_effort', 'enable_thinking',
                   'chat_template_kwargs', 'reasoning_split'):
        from lib.llm_dispatch.provider_registry import get_dialect
        if get_dialect(fmt) is None:
            raise ReasoningContractError('unknown_dialect', 'destination dialect is not registered')
    efforts = reasoning.get('supported_efforts', [])
    resolved = None
    if mode == 'exact':
        resolved = value
        if resolved is None or resolved not in efforts:
            raise ReasoningContractError('unsupported_effort', 'exact effort cannot be represented')
    elif mode == 'preset':
        resolved, _ = _clamp_preset(value, efforts, reasoning)
    elif mode == 'lowest':
        resolved = _cheapest_rung(efforts, reasoning)
    elif mode == 'enabled':
        resolved = reasoning.get('default_effort')
        if resolved is None:
            resolved = _cheapest_rung(efforts, reasoning)
    elif mode == 'off':
        resolved = reasoning.get('disabled_effort')
        if resolved is None and 'none' in efforts:
            resolved = 'none'
    if resolved is not None and resolved not in efforts:
        raise ReasoningContractError('unsupported_effort', 'requested effort is not in destination ladder')
    if enabled and resolved is not None and resolved in ('none', reasoning.get('disabled_effort')):
        raise ReasoningContractError('unsupported_effort', 'an enabled request resolved to a disabled effort')
    if fmt == 'reasoning_effort' and resolved is None:
        raise ReasoningContractError('unrepresentable_reasoning', 'destination has no effort for this intent')
    return fmt, enabled, resolved, mode


# Ranking is only for the composer restoration projection (degraded choices
# are already the executable contract in resolve_reasoning_control above).
_RESTORABLE_DEPTHS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'lowest', 'enabled', 'none')
_RANKED_DEPTHS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')


def clamp_reasoning_depth(selectable, depth):
    """Restore against final executable controls, after route intersection.

    Native names (including ultra) win exactly. Known unavailable efforts move
    down, off/lowest choose the lightest enabled rung, and unrankable preferences
    return to Default. Unknown/unroutable capabilities must not call this policy.
    """
    if depth == 'default' or depth in selectable:
        return depth
    rungs = [d for d in selectable if d not in ('default', 'off', 'lowest')]
    if depth in ('off', 'lowest', 'none'):
        return 'off' if depth == 'none' and 'off' in selectable else next(iter(rungs), 'default')
    if depth not in _RANKED_DEPTHS:
        return 'default'
    below = [d for d in rungs if d in _RANKED_DEPTHS
             and _RANKED_DEPTHS.index(d) <= _RANKED_DEPTHS.index(depth)]
    return max(below, key=_RANKED_DEPTHS.index) if below else 'default'


def reasoning_depth_restoration(selectable):
    """Bounded server projection; the browser applies this table, never ranks."""
    return {'fallback': 'default', 'replacements': {
        depth: clamp_reasoning_depth(selectable, depth)
        for depth in _RESTORABLE_DEPTHS if depth not in selectable}}


def defer_reasoning_compilation(body):
    """Stage a canonical intent when no destination is bound; never a wire body.

    Task-level cross-model rebuilds may normalize messages before selection, but
    only the selected slot may decide whether its profile can execute the intent.
    The pending marker is a fail-closed transport guard, not a capability grant.
    """
    working = dict(body)
    intent = working.get('_reasoning_intent')
    if intent is None:
        intent = native_intent(working)
    _validate_intent(intent)
    working['_reasoning_intent'] = copy.deepcopy(intent)
    _clear(working)
    working.update(copy.deepcopy(intent.get('sampling', {})))
    for key in ('_route_inference_profile', '_inference_profile', '_reasoning_resolution'):
        working.pop(key, None)
    working['_reasoning_pending'] = True
    body.clear()
    body.update(working)
    return body


def compile_reasoning(body, model, *, inference_profile=None, thinking_format=''):
    """Commit a fresh destination projection only after successful compilation."""
    working = dict(body)
    if inference_profile is not None:
        from lib.model_routing.inference import validate_inference_profile
        from lib.model_routing.domain import ModelRoutingError
        try:
            inference_profile = validate_inference_profile(inference_profile)
        except ModelRoutingError:
            raise ReasoningContractError('invalid_profile', 'destination profile is invalid') from None
        working['_route_inference_profile'] = inference_profile
    else:
        working.pop('_route_inference_profile', None)
    # Plugins may touch nested input. Isolate them to keep failure atomic,
    # without cloning long histories on the ordinary built-in hot path.
    fmt = (inference_profile or {}).get('reasoning', {}).get('wire_format') or thinking_format
    if fmt and fmt not in ('none', 'thinking_type', 'enable_thinking',
                           'reasoning_effort', 'reasoning_split', 'chat_template_kwargs'):
        working = copy.deepcopy(working)
    working.pop('_inference_profile', None)
    _compile(working, model, inference_profile=inference_profile, thinking_format=thinking_format)

    working.pop('_reasoning_pending', None)
    body.clear()
    body.update(working)
    return body


def _compile(body, model, *, inference_profile, thinking_format):
    if '_reasoning_intent' not in body:
        body['_reasoning_intent'] = native_intent(body)
    _validate_intent(body['_reasoning_intent'])
    intent = copy.deepcopy(body['_reasoning_intent'])
    body['_reasoning_intent'] = intent
    mode, value = intent['mode'], intent.get('value')
    requested = mode
    _clear(body)
    body.update(copy.deepcopy(intent.get('sampling', {})))
    source = 'profile' if inference_profile is not None else 'legacy'
    effective, reason = mode, 'matched'
    r = (inference_profile or {}).get('reasoning', {})
    # Old template declarations identify a dialect, not a capability. Retain
    # compatibility for those older templates, but a declared support state
    # always selects the strict compiler. Exact/off choices remain non-lossy.
    legacy_unconfigured = not r.get('support')
    if inference_profile is None or legacy_unconfigured:
        from lib.llm._reasoning_legacy import apply_legacy
        source = 'legacy'
        thinking_format = r.get('wire_format') or thinking_format
        try:
            legacy_value = value or 'medium'
            if mode == 'preset' and not intent.get('strict') and legacy_value in {'opus', 'qwen', 'gemini', 'minimax', 'doubao', 'off', 'low'}:
                legacy_value = 'medium'
            if mode != 'default':
                temperature = body.pop('temperature', 1.0)
                apply_legacy(body, model, thinking_format, mode != 'off',
                             temperature, legacy_value)
        except Exception as exc:
            raise ReasoningContractError('plugin_failure', 'compatibility dialect failed') from exc
        actual = native_intent(body)
        if mode == 'off' and actual['mode'] != 'off':
            effective, reason = actual['mode'], 'disable_not_verified'
            # Unconfigured historical callers retain their existing floor policy.
            # Explicit profiles below never degrade an off request.

            if inference_profile is not None and intent.get('strict'):
                raise ReasoningContractError('cannot_disable', 'destination cannot guarantee reasoning off')
        if inference_profile is not None and intent.get('strict'):
            if mode == 'exact' and (actual['mode'] != 'exact' or actual['value'] != value):
                raise ReasoningContractError('unsupported_effort', 'destination cannot represent exact effort')
            if mode in {'enabled', 'lowest', 'preset'} and actual['mode'] in {'off', 'default'}:
                raise ReasoningContractError('unsupported_reasoning', 'destination reasoning support is not declared')
    else:
        r = inference_profile.get('reasoning', {})
        fmt = r.get('wire_format', 'none')
        support = r.get('support', 'unknown')

        if mode == 'off' and not intent.get('strict') and support == 'unknown':
            mode, effective, reason = 'default', 'default', 'disable_not_verified'
        fmt, enabled, resolved, mode = resolve_reasoning_control(
            r, mode, value, strict=bool(intent.get('strict', True)))
        if requested == 'off' and enabled and reason == 'matched':
            # resolve_reasoning_control degraded a disable request the
            # destination cannot honor into the smallest reasoning the model
            # actually does (the cheapest rung, or a bare enabled control when
            # the model has no effort ladder) instead of failing the turn.
            effective, reason = 'lowest', 'disable_unavailable_fell_back_to_lowest'
        elif requested == 'preset' and value is not None and resolved != value \
                and reason == 'matched':
            # The requested preset is absent from the destination ladder;
            # the resolver mapped it onto the nearest representable rung.
            effective, reason = 'preset', 'preset_clamped_to_representable'
        if mode != 'default' and support != 'unsupported':
            if fmt == 'thinking_type':
                # Route extras cannot override the compiled on/off discriminator.
                body['thinking'] = {'type': r.get('enabled_type', 'enabled') if enabled else 'disabled'}
                if enabled:
                    body['thinking'].update(copy.deepcopy(r.get('thinking_extra', {})))
                    body['thinking']['type'] = r.get('enabled_type', 'enabled')
                    if r.get('display'):
                        body['thinking']['display'] = r['display']
                    if r.get('preserve_history') == 'keep_all':
                        body['thinking']['keep'] = 'all'
                    if r.get('preserve_history') == 'clear_thinking_false' and any(
                            m.get('reasoning_content') for m in body.get('messages', []) if isinstance(m, dict)):
                        body['thinking']['clear_thinking'] = False
                    budget = r.get('default_budget_tokens')
                    if budget is not None:
                        body['thinking']['budget_tokens'] = max(r.get('min_budget_tokens') or 0, min(budget, r.get('max_budget_tokens') or budget))
            elif fmt in ('enable_thinking', 'reasoning_split'):
                body[fmt] = enabled
            elif fmt == 'chat_template_kwargs':
                body.setdefault('chat_template_kwargs', {})['enable_thinking'] = enabled
            elif fmt not in ('none', 'reasoning_effort'):
                from lib.llm_dispatch.provider_registry import get_dialect
                plugin = get_dialect(fmt)
                if plugin is None:
                    raise ReasoningContractError('unknown_dialect', 'destination dialect is not registered')
                try:
                    plugin.apply_build(body, thinking_enabled=enabled, temperature=body.get('temperature'), model=model, effort=resolved)
                except Exception as exc:
                    raise ReasoningContractError('plugin_failure', 'destination dialect failed') from exc
            if resolved is not None and (enabled or fmt == 'reasoning_effort'):
                body[r.get('effort_field', 'reasoning_effort')] = resolved
            if fmt == 'none' and mode != 'default':
                raise ReasoningContractError('unrepresentable_reasoning', 'destination has no reasoning control')
        if mode == 'default' and fmt == 'reasoning_effort' and resolved is not None:
            # A default on the effort-ladder wire is emitted explicitly (see
            # resolve_reasoning_control); every other format keeps omission.
            body[r.get('effort_field', 'reasoning_effort')] = resolved
    _apply_sampling(body, (inference_profile or {}).get('sampling', {}), mode)

    budget = intent.get('budget_tokens')
    if budget is not None:
        thinking = body.get('thinking')
        if (mode in ('off', 'default') or not isinstance(thinking, dict)
                or thinking.get('type') != 'enabled'):
            raise ReasoningContractError('unrepresentable_budget', 'destination cannot represent an explicit reasoning budget')
        if not r.get('min_budget_tokens', 1) <= budget <= r.get('max_budget_tokens', 1000000):
            raise ReasoningContractError('unsupported_budget', 'requested reasoning budget exceeds destination bounds')
        thinking['budget_tokens'] = budget
    actual = native_intent(body)
    body['_reasoning_resolution'] = {
        'requested': requested, 'effective': effective,
        'source': source, 'reason': reason,
        'requestedValue': str(value) if value is not None else '',
        'actualMode': actual['mode'],
        'actualValue': str(actual.get('value') or ''),
    }
    return body


def _apply_sampling(body, sampling, mode):
    enabled = mode not in ('off', 'default')
    key = 'enabled_temperature' if enabled else 'disabled_temperature'
    temp = sampling.get(key) if mode != 'default' else None
    temp = sampling.get('temperature') if temp is None else temp
    if temp == 'omit':
        body.pop('temperature', None)
    elif temp is not None and temp != 'allow':
        body['temperature'] = temp
    if body.get('temperature') is not None:
        for key, op in (('min_temperature', max), ('max_temperature', min)):
            if sampling.get(key) is not None:
                body['temperature'] = op(body['temperature'], sampling[key])
    for key in set(sampling.get('omit_params', [])) | set(sampling.get('omit', [])):
        body.pop(key, None)
