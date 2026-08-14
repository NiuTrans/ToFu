"""lib/conv_config/_resolve.py — the two big public config/settings resolvers.

Server-side port of ``static/js/main.js:_buildConvConfig`` and
``_buildConvSettings``.
"""

from __future__ import annotations

from typing import Mapping, Optional

from lib.log import get_logger

from lib.conv_config._flow import _parse_active_flow
from lib.conv_config._legacy import (
    canonicalise_model_id,
    extract_legacy_thinking_depth,
)
from lib.conv_config._util import _coerce_bool, _first_defined, _pick

logger = get_logger(__name__)


def resolve_conv_config(
    conv_settings: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
    server_defaults: Optional[Mapping] = None,
    *,
    is_active: bool = True,
) -> dict:
    """Resolve the runtime config dict that goes to chat task endpoints.

    Mirrors the JS ``_buildConvConfig`` field-by-field.

    Returns a fresh dict (caller may mutate freely).
    """
    conv = dict(conv_settings or {})
    ov = dict(overrides or {})
    defaults = dict(server_defaults or {})

    # Convenience:
    server_model = defaults.get('serverModel') or ''

    # Model: active uses ov.model || serverModel; inactive uses
    # conv.model || serverModel.
    model_active_raw = _first_defined(ov.get('model'), server_model) or ''
    model_inactive_raw = _first_defined(conv.get('model'), server_model) or ''
    model_raw = _pick(model_active_raw, model_inactive_raw,
                       is_active=is_active)
    # Canonicalise legacy preset names ("opus" / "high" / "qwen" / etc.)
    # to actual model_ids so chat-pipeline routing always sees a real id.
    model = canonicalise_model_id(model_raw)
    # Extract a depth from the same value when it was a compound preset
    # (matches JS: `if (['medium','high','xhigh','max'].includes(config.preset)
    # && !config.thinkingDepth) config.thinkingDepth = config.preset`).
    legacy_depth = extract_legacy_thinking_depth(model_raw)

    # ── Field-by-field, matching the JS impl exactly ──
    out = {
        # ROOT-CAUSE guard: never hand a None maxTokens to any consumer.
        # ``.get(k, default)`` only substitutes for an ABSENT key, so
        # ``ov.get('maxTokens', defaults.get('maxTokens'))`` returned None
        # whenever NEITHER overrides NOR server_defaults carried the key —
        # the exact shape the killed-turn recovery path produces
        # (resolve_conv_config(conv_settings=…, is_active=False) with no
        # overrides/defaults). That None propagated to build_body →
        # _clamp_max_tokens → ``min(None, int)`` and FATALed the whole turn
        # ("'<' not supported between instances of 'int' and 'NoneType'"),
        # which the killed-recovery sweep then re-dispatched into a crash
        # loop. Coerce a missing/None/invalid value to the same 128000 the
        # downstream resolver defaults to — behaviourally identical for every
        # reader (all already do ``cfg.get('maxTokens') or <fallback>``), and
        # it eliminates the invariant violation at the source. The two
        # downstream coercions (_resolve_model_config, _clamp_max_tokens)
        # remain as defense-in-depth.
        'maxTokens': ov.get('maxTokens') or defaults.get('maxTokens') or 128000,
        'thinkingEnabled': _coerce_bool(ov.get('thinkingEnabled'),
                                          defaults.get('thinkingEnabled', False)),
        'model': model,
        'preset': model,
        'systemPrompt': ov.get('systemPrompt') or defaults.get('systemPrompt') or '',
        # 'append' (default) → user prompt is prepended ON TOP of the
        # built-in Claude-Code static prompt. 'replace' → user prompt
        # fully replaces the built-in base block (CLAUDE.md / memory /
        # swarm / date are still auto-injected — they track feature
        # toggles, not the base prose). See _inject_system_contexts.
        'systemPromptMode': (
            ov.get('systemPromptMode')
            or defaults.get('systemPromptMode')
            or 'append'),
        # Per-block keep/drop toggles from the system-prompt editor.
        # Shape: {'disabled': [block_id, ...]}. Global (not per-conv),
        # so it reads from overrides → server defaults like systemPrompt.
        'systemPromptBlocks': (
            ov.get('systemPromptBlocks')
            or defaults.get('systemPromptBlocks')
            or {}),
        'thinkingDepth': _pick(
            ov.get('thinkingDepth') or legacy_depth,
            conv.get('thinkingDepth') or legacy_depth or None,
            is_active=is_active,
        ),
        'temperature': ov.get('temperature', defaults.get('temperature')),
        'searchMode': _pick(
            ov.get('searchMode'),
            conv.get('searchMode') or 'multi',
            is_active=is_active,
        ),
        'fetchEnabled': _pick(
            _coerce_bool(ov.get('fetchEnabled'), True),
            _coerce_bool(conv.get('fetchEnabled'), False),
            is_active=is_active,
        ),
        'codeExecEnabled': _pick(
            _coerce_bool(ov.get('codeExecEnabled')),
            _coerce_bool(conv.get('codeExecEnabled')),
            is_active=is_active,
        ),
        'memoryEnabled': _pick(
            _coerce_bool(ov.get('memoryEnabled'), True),
            (_coerce_bool(conv.get('memoryEnabled'), True)
             if conv.get('memoryEnabled') is not None
             else True),
            is_active=is_active,
        ),
        'schedulerEnabled': _pick(
            _coerce_bool(ov.get('schedulerEnabled')),
            _coerce_bool(conv.get('schedulerEnabled')),
            is_active=is_active,
        ),
        'swarmEnabled': _pick(
            _coerce_bool(ov.get('swarmEnabled')),
            _coerce_bool(conv.get('swarmEnabled')),
            is_active=is_active,
        ),
        'projectPath': ov.get('projectPath') or conv.get('projectPath') or '',
        # RWA 伪路径(remote:<agent>:<root>)在下方翻译为 project_remote。
        'project_remote': None,
        'projectPaths': list(conv.get('projectPaths') or []),
        'readOnlyPaths': list(conv.get('readOnlyPaths') or []),
        'autoApply': _coerce_bool(ov.get('autoApply'), False),
        'browserEnabled': _pick(
            _coerce_bool(ov.get('browserEnabled')),
            _coerce_bool(conv.get('browserEnabled')),
            is_active=is_active,
        ),
        'desktopEnabled': _pick(
            _coerce_bool(ov.get('desktopEnabled')),
            _coerce_bool(conv.get('desktopEnabled')),
            is_active=is_active,
        ),
        'imageGenEnabled': _pick(
            _coerce_bool(ov.get('imageGenEnabled')),
            _coerce_bool(conv.get('imageGenEnabled')),
            is_active=is_active,
        ),
        'humanGuidanceEnabled': _pick(
            _coerce_bool(ov.get('humanGuidanceEnabled')),
            _coerce_bool(conv.get('humanGuidanceEnabled')),
            is_active=is_active,
        ),
        'endpointMode': _pick(
            _coerce_bool(ov.get('endpointMode')),
            _coerce_bool(conv.get('endpointEnabled')),
            is_active=is_active,
        ),
        'autopilot': _pick(
            _coerce_bool(ov.get('autopilot')),
            _coerce_bool(conv.get('autopilotEnabled')),
            is_active=is_active,
        ),
        'autoTranslate': (
            _coerce_bool(conv.get('autoTranslate'),
                          _coerce_bool(ov.get('autoTranslate'), False))
            if conv.get('autoTranslate') is not None
            else _coerce_bool(ov.get('autoTranslate'), False)
        ),
        # LLM-correction tier of the input language detector. UI default ON
        # (personal_scope.ui_default=True); it only ever fires when
        # autoTranslate is also on AND the statistical detection is ambiguous,
        # so the blast radius is bounded. Headless surfaces get it forced OFF
        # by apply_headless_personal_defaults (fail-closed).
        'langCorrectionEnabled': _coerce_bool(
            ov.get('langCorrectionEnabled'),
            (_coerce_bool(conv.get('langCorrectionEnabled'), True)
             if conv.get('langCorrectionEnabled') is not None else True)),
        # OUTPUT-side translate target language (model reply → human). The
        # frontend ships _i18nLang here so the server-side safety net + the
        # incremental translator render into the UI language instead of the
        # old Chinese hard-pin. Absent/None (headless / old frontend) →
        # resolve_translate_target falls back to Chinese. Global toolbar value
        # wins for the active conv; stored per-conv value for an inactive one.
        'uiLang': _pick(ov.get('uiLang'), conv.get('uiLang'),
                        is_active=is_active) or None,
        'browserClientId': None,  # populated below
        'keepToolHistory': ov.get('keepToolHistory') is not False,
    }
    # ── Orchestration flow selection (the Mode dropdown) ──
    # Active conv reads the live toolbar token; inactive reads the stored one.
    active_flow = _pick(ov.get('activeFlow'), conv.get('activeFlow'),
                        is_active=is_active)
    flow_builtin, flow_id = _parse_active_flow(active_flow)
    # ── Two-tier chat mode (chat/studio) ──
    # Passthrough only: the authoritative expansion into atomic flags happens
    # ONCE downstream in _resolve_model_config (via chat_mode.apply_chat_mode,
    # which also normalises legacy air/pro → chat), so the tier and the flags
    # can never diverge. Active conv reads the live toolbar value; an inactive
    # one reads the stored per-conv value.
    _chat_mode = _pick(ov.get('chatMode'), conv.get('chatMode'),
                       is_active=is_active)
    if isinstance(_chat_mode, str) and _chat_mode.strip():
        out['chatMode'] = _chat_mode.strip().lower()
    out['activeFlow'] = active_flow if isinstance(active_flow, str) else ''
    out['flowBuiltin'] = flow_builtin
    out['flowId'] = flow_id
    # browserClientId is gated on the resolved browserEnabled flag.
    if out['browserEnabled']:
        out['browserClientId'] = ov.get('browserClientId') or None
    # Global, request-local Responses experiments. They are not conversation
    # content and therefore do not persist into settings; the live toolbar
    # snapshot applies equally to active and background conversations.
    for block in ('cache', 'tools', 'responses', 'compaction'):
        value = ov.get(block)
        if not isinstance(value, Mapping):
            value = defaults.get(block)
        if isinstance(value, Mapping):
            out[block] = dict(value)
    # ── RWA remote-worktree pseudo-path (总闸 TOFU_REMOTE_WORKTREE) ──
    # conv.projectPath = 'remote:<agent_id>:<root>' rides the existing
    # per-conv persistence untouched; HERE it becomes the binding contract
    # (cfg['project_remote']) and the server-side projectPath is cleared —
    # there is no such path on the server. Master switch off → byte-identical.
    from lib.desktop.remote import parse_remote_path, remote_worktree_enabled
    if remote_worktree_enabled():
        parsed = parse_remote_path(out['projectPath'])
        if parsed:
            out['project_remote'] = {'agent_id': parsed[0], 'root': parsed[1]}
            out['projectPath'] = ''
    return out


def resolve_conv_settings(
    conv_settings: Optional[Mapping] = None,
    overrides: Optional[Mapping] = None,
) -> dict:
    """Resolve the per-conversation settings dict for server persistence.

    Mirrors the JS ``_buildConvSettings`` field-by-field. Used as the
    ``settings`` payload on chat-send / regenerate / continue / branch
    POSTs and on PATCH ``/api/chat/tool-state``.
    """
    conv = dict(conv_settings or {})
    ov = dict(overrides or {})
    server_model = ov.get('serverModel') or ''

    raw_model = conv.get('model') or ov.get('model') or server_model
    model = canonicalise_model_id(raw_model)
    legacy_depth = extract_legacy_thinking_depth(raw_model)

    out = {
        'model': model,
        'preset': model,
        'thinkingDepth': (conv.get('thinkingDepth')
                          or ov.get('thinkingDepth')
                          or legacy_depth),
        'searchMode': conv.get('searchMode') or ov.get('searchMode') or 'multi',
        'fetchEnabled': _coerce_bool(conv.get('fetchEnabled')),
        'codeExecEnabled': _coerce_bool(conv.get('codeExecEnabled')),
        'browserEnabled': _coerce_bool(conv.get('browserEnabled')),
        'desktopEnabled': _coerce_bool(conv.get('desktopEnabled')),
        'memoryEnabled': (
            _coerce_bool(conv.get('memoryEnabled'), True)
            if conv.get('memoryEnabled') is not None else True
        ),
        'schedulerEnabled': _coerce_bool(conv.get('schedulerEnabled')),
        'swarmEnabled': _coerce_bool(conv.get('swarmEnabled')),
        'endpointEnabled': _coerce_bool(conv.get('endpointEnabled')),
        'autopilotEnabled': _coerce_bool(conv.get('autopilotEnabled')),
        'imageGenEnabled': _coerce_bool(conv.get('imageGenEnabled')),
        'humanGuidanceEnabled': _coerce_bool(conv.get('humanGuidanceEnabled')),
        'activeFlow': (conv.get('activeFlow') if isinstance(conv.get('activeFlow'), str)
                       else (ov.get('activeFlow') if isinstance(ov.get('activeFlow'), str) else '')),
        'projectPath': conv.get('projectPath') or '',
        'projectPaths': list(conv.get('projectPaths') or []),
        'readOnlyPaths': list(conv.get('readOnlyPaths') or []),
        'autoTranslate': (
            _coerce_bool(conv.get('autoTranslate'),
                          _coerce_bool(ov.get('autoTranslate'), False))
            if conv.get('autoTranslate') is not None
            else _coerce_bool(ov.get('autoTranslate'), False)
        ),
        'folderId': conv.get('folderId') or None,
        # OUTPUT-side translate target language (see resolve_conv_config). This
        # is what _maybe_auto_translate_assistant reads off settings.uiLang.
        'uiLang': conv.get('uiLang') or ov.get('uiLang') or None,
    }
    # Two-tier chat mode — persist the stored/override value (chat/studio). A
    # legacy air/pro value is left as-is here and normalised forward to 'chat'
    # downstream by chat_mode.normalize_chat_mode at resolve time.
    _cm = conv.get('chatMode') or ov.get('chatMode')
    if isinstance(_cm, str) and _cm.strip():
        out['chatMode'] = _cm.strip().lower()
    return out
