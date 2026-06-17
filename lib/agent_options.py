"""lib/agent_options.py — Typed configuration object for an agent run.

Today, ``cfg`` is a free-form dict that flows from the route layer
(``routes/api_v1/chat.py``, ``routes/chat.py``, OpenAI/Anthropic compat
adapters) into ``lib.tasks_pkg.orchestrator`` + ``model_config``.  About
30 distinct keys are read across the codebase, all silently — typos pass
through, the wire schema is implicit, and SDK callers can't discover
what's tunable without grepping our source.

This module provides ``TofuOptions``: a single typed dataclass with
explicit field definitions, type coercion, and a stable mapping between
camelCase wire format (what the frontend / SDKs / OpenAPI schema use)
and snake_case Python attributes.

Design constraints
------------------
* **Backward-compatible**: ``TofuOptions.from_cfg(d)`` accepts ANY existing
  cfg dict and ``to_cfg()`` round-trips back to the camelCase shape the
  orchestrator already reads.  Unknown keys survive in ``extras`` and are
  re-emitted unchanged — we don't break existing callers.
* **Additive validation**: unknown keys log a debug line (typo hint) but
  never reject the request.  Hard rejection would silently break Feishu /
  scheduler / autopilot callers that pass ad-hoc keys.
* **Single source of truth for OpenAPI**: ``TofuOptions.openapi_schema()``
  generates the ``TofuConfig`` component used in ``lib/openapi.py``.

Usage
-----
::

    from lib.agent_options import TofuOptions

    # Parse a request body
    opts = TofuOptions.from_cfg(request_body.get('config') or {})

    # Read typed fields
    if opts.swarm_enabled and opts.project_path:
        ...

    # Hand to the orchestrator (which still wants a dict)
    cfg_for_orchestrator = opts.to_cfg()
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ── Wire ↔ Python name table ───────────────────────────────────────
#
# Most camelCase → snake_case conversions are mechanical, but a few have
# legacy aliases (``preset`` accepts ``effort``; ``user`` is plain).  The
# table is the single source of truth — adding a new field means adding
# one row here and one dataclass attribute.

# (camelCase wire name, snake_case attr name, type, default,
#  enum/None, description)
_FIELDS: list[tuple[str, str, type, Any, Optional[tuple], str]] = [
    # ── Model & generation ────────────────────────────────────────
    ('model',                 'model',                 str,   '',     None, 'Model id (e.g. claude-opus-4-7).'),
    ('preset',                'preset',                str,   '',     None, 'Preset / effort tier. Frontend now sends model_id directly here.'),
    ('maxTokens',             'max_tokens',            int,   128000, None, 'Max output tokens per call.'),
    ('temperature',           'temperature',           float, 1.0,    None, 'Sampling temperature.'),
    ('thinkingEnabled',       'thinking_enabled',      bool,  None,   None, 'Enable extended thinking.  None = use model/preset default.'),
    ('thinkingDepth',         'thinking_depth',        str,   '',
        ('', 'off', 'low', 'medium', 'high', 'xhigh', 'max'),
        'Thinking-budget tier.'),
    ('defaultThinkingDepth',  'default_thinking_depth', str,  'medium',
        ('off', 'low', 'medium', 'high', 'xhigh', 'max'),
        'User-configured default depth (used when thinking_depth is empty).'),

    # ── Tool toggles ──────────────────────────────────────────────
    ('searchMode',            'search_mode',           str,   'multi',
        ('off', 'single', 'multi'),
        'Web-search behaviour.'),
    ('fetchEnabled',          'fetch_enabled',         bool,  True,   None, 'Enable fetch_url tool.'),
    ('codeExecEnabled',       'code_exec_enabled',     bool,  False,  None, 'Enable sandboxed code execution.'),
    ('memoryEnabled',         'memory_enabled',        bool,  True,   None, 'Enable memory tools.'),
    ('browserEnabled',        'browser_enabled',       bool,  False,  None, 'Enable browser-extension tools.'),
    ('desktopEnabled',        'desktop_enabled',       bool,  False,  None, 'Enable desktop-agent tools.'),
    ('swarmEnabled',          'swarm_enabled',         bool,  False,  None, 'Enable spawn_agents / check_agents.'),
    ('imageGenEnabled',       'image_gen_enabled',     bool,  False,  None, 'Enable image-generation tool.'),
    ('humanGuidanceEnabled',  'human_guidance_enabled', bool, False,  None, 'Enable ask_human tool.'),
    ('schedulerEnabled',      'scheduler_enabled',     bool,  False,  None, 'Enable scheduler tools.'),
    ('mcpEnabled',            'mcp_enabled',           bool,  True,   None, 'Bridge external MCP servers.'),

    # ── Project / paths ───────────────────────────────────────────
    ('projectPath',           'project_path',          str,   '',     None, 'Primary project directory.'),
    ('projectPaths',          'project_paths',         list,  None,   None, 'Multi-root project paths.'),
    ('readOnlyPaths',         'read_only_paths',       list,  None,   None, 'Subset of project paths registered read-only (no writes/edits).'),

    # ── Caller-supplied overrides ─────────────────────────────────
    ('tools',                 'tools',                 list,  None,   None,
        'Caller-supplied tool list (OpenAI shape).  When non-empty, '
        'auto-derived tools are disabled.'),
    ('messages',              'messages',              list,  None,   None,
        'Inline message override.  Normally messages live on the task, '
        'not in cfg — set by route adapters.'),
    ('toolHistory',           'tool_history',          list,  None,   None, 'Pre-existing tool round history.'),

    # ── Backend / mode ────────────────────────────────────────────
    ('profile',               'profile',               str,   '',     None,
        'Capability profile name (lib/agent_core/profiles.py). Supplies cfg '
        'defaults that explicit keys override. Empty / "default" = no-op.'),
    ('agentBackend',          'agent_backend',         str,   'builtin',
        ('builtin', 'codex', 'claude_code'),
        'Which agent runtime to use.'),
    ('endpointMode',          'endpoint_mode',         bool,  False,  None, 'Planner→Worker→Critic loop.'),
    ('autopilot',             'autopilot',             bool,  False,  None, 'Autopilot mode.'),

    # ── Safety / limits ───────────────────────────────────────────
    ('maxBudgetUsd',          'max_budget_usd',        float, 0.0,    None,
        'Hard $ ceiling on accumulated token cost; 0 = no budget gate. '
        'Inspired by Claude Agent SDK\'s max_budget_usd.'),
    ('disableModelFallback',  'disable_model_fallback', bool, False,  None,
        'Opt OUT of automatic model fallback for THIS request. The server '
        'admin configures a global fallback model (Settings → model '
        'defaults); when the primary model errors, the round is silently '
        're-run on that fallback model. Headless callers who pin a model '
        '(reproducible runs, benchmarks, evals) usually do NOT want this '
        'silent switch — set true to surface the primary error instead '
        '(error envelope context="fallback-disabled"). Default false = '
        'inherit the server behaviour.'),

    # ── Misc / advanced ───────────────────────────────────────────
    ('autoApply',             'auto_apply',            bool,  True,   None, 'Apply edits without prompting.'),
    ('keepToolHistory',       'keep_tool_history',     bool,  True,   None, 'Persist tool rounds in conversation.'),
    ('contentPrefix',         'content_prefix',        str,   '',     None, 'Prefix injected before assistant content.'),
    ('browserClientId',       'browser_client_id',     str,   '',     None, 'Browser-extension client id.'),
    ('excludeLast',           'exclude_last',          bool,  False,  None, 'Exclude last message from context (regen).'),
    ('user',                  'user_id',               str,   '',     None, 'OpenAI-compat user identifier (passthrough).'),

    # ── Checkpoint controls (advanced) ────────────────────────────
    ('checkpointToolRounds',  'checkpoint_tool_rounds', list, None,   None, 'Tool rounds to checkpoint to disk.'),
    ('checkpointUsage',       'checkpoint_usage',      bool,  False,  None, 'Persist usage to checkpoint.'),
    ('checkpointApiRounds',   'checkpoint_api_rounds', bool,  False,  None, 'Persist API rounds to checkpoint.'),
    ('checkpointModifiedFiles', 'checkpoint_modified_files', bool, False, None, 'Persist modified-files map.'),
    ('checkpointModifiedFileList', 'checkpoint_modified_file_list', bool, False, None, 'Persist modified-file list.'),
]


_WIRE_TO_ATTR = {wire: attr for wire, attr, *_ in _FIELDS}
_ATTR_TO_WIRE = {attr: wire for wire, attr, *_ in _FIELDS}
_ATTR_TO_TYPE = {attr: typ for _, attr, typ, *_ in _FIELDS}
_LEGACY_ALIASES = {
    'effort': 'preset',  # legacy: cfg['effort'] is read alongside cfg['preset']
}


def _coerce(value: Any, typ: type) -> Any:
    """Best-effort coerce ``value`` to ``typ``.  Returns the original value on failure."""
    if value is None:
        return None
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if typ is int:
        try:
            return int(value)
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[agent_options] _coerce caught %s: %s', type(_e_audit).__name__, _e_audit)
            return value
    if typ is float:
        try:
            return float(value)
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[agent_options] _coerce caught %s: %s', type(_e_audit).__name__, _e_audit)
            return value
    if typ is str:
        return value if isinstance(value, str) else str(value)
    if typ is list:
        return value if isinstance(value, list) else value
    return value


# ── The dataclass ──────────────────────────────────────────────────


@dataclass
class TofuOptions:
    """Typed agent-run options.

    Attribute names are snake_case Python; the wire format is camelCase.
    Round-trip with ``from_cfg`` / ``to_cfg``.
    """
    # Model & generation
    model: str = ''
    preset: str = ''
    max_tokens: int = 128000
    temperature: float = 1.0
    thinking_enabled: Optional[bool] = None
    thinking_depth: str = ''
    default_thinking_depth: str = 'medium'

    # Tool toggles
    search_mode: str = 'multi'
    fetch_enabled: bool = True
    code_exec_enabled: bool = False
    memory_enabled: bool = True
    browser_enabled: bool = False
    desktop_enabled: bool = False
    swarm_enabled: bool = False
    image_gen_enabled: bool = False
    human_guidance_enabled: bool = False
    scheduler_enabled: bool = False
    mcp_enabled: bool = True

    # Project / paths
    project_path: str = ''
    project_paths: Optional[list] = None
    read_only_paths: Optional[list] = None

    # Caller-supplied overrides
    tools: Optional[list] = None
    messages: Optional[list] = None
    tool_history: Optional[list] = None

    # Backend / mode
    profile: str = ''
    agent_backend: str = 'builtin'
    endpoint_mode: bool = False
    autopilot: bool = False

    # Safety / limits
    max_budget_usd: float = 0.0
    disable_model_fallback: bool = False

    # Misc / advanced
    auto_apply: bool = True
    keep_tool_history: bool = True
    content_prefix: str = ''
    browser_client_id: str = ''
    exclude_last: bool = False
    user_id: str = ''

    # Checkpoint controls
    checkpoint_tool_rounds: Optional[list] = None
    checkpoint_usage: bool = False
    checkpoint_api_rounds: bool = False
    checkpoint_modified_files: bool = False
    checkpoint_modified_file_list: bool = False

    # Pass-through bag for keys we don't yet model.  Re-emitted on to_cfg().
    extras: dict = field(default_factory=dict)

    # ── Construction ─────────────────────────────────────────────

    @classmethod
    def from_cfg(cls, cfg: Optional[dict]) -> 'TofuOptions':
        """Parse a wire-format dict into a typed options object.

        Unknown keys are kept in ``extras`` and re-emitted on ``to_cfg()``
        so callers using bleeding-edge or feature-flag keys aren't broken.
        """
        if not cfg:
            return cls()
        if not isinstance(cfg, dict):
            logger.warning('[TofuOptions] from_cfg: expected dict, got %s — ignored',
                           type(cfg).__name__)
            return cls()

        kwargs: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for wire_key, raw_val in cfg.items():
            attr = _WIRE_TO_ATTR.get(wire_key)
            if attr is None:
                attr = _LEGACY_ALIASES.get(wire_key)
            if attr is None:
                extras[wire_key] = raw_val
                continue
            typ = _ATTR_TO_TYPE.get(attr)
            if typ is None:
                kwargs[attr] = raw_val
                continue
            kwargs[attr] = _coerce(raw_val, typ)

        if extras:
            logger.debug('[TofuOptions] %d unknown cfg key(s): %s',
                         len(extras), ', '.join(sorted(extras.keys()))[:200])
            kwargs['extras'] = extras

        try:
            return cls(**kwargs)
        except TypeError as e:
            logger.warning('[TofuOptions] from_cfg dataclass init failed: %s — '
                           'falling back to empty options + full extras', e)
            return cls(extras=dict(cfg))

    # ── Emission ─────────────────────────────────────────────────

    def to_cfg(self) -> dict:
        """Emit a wire-format (camelCase) dict the orchestrator consumes.

        Defaults are NOT pruned — the orchestrator's own ``cfg.get('foo',
        default)`` calls handle that, and emitting explicit values keeps
        the round-trip lossless when an SDK caller wants to inspect what
        the server inferred.
        """
        out: dict[str, Any] = dict(self.extras)  # extras first, real fields override

        for wire, attr, typ, default, *_rest in _FIELDS:
            value = getattr(self, attr)
            # Skip None when the dataclass default is also None — keeps
            # the dict tight and lets orchestrator .get(default) win.
            if value is None and default is None:
                continue
            out[wire] = value

        # Legacy alias: if preset is set but extras still has 'effort',
        # let preset win (preset is read first in orchestrator).
        out.pop('effort', None)
        return out

    # ── Schema ───────────────────────────────────────────────────

    @classmethod
    def openapi_schema(cls) -> dict:
        """Return an OpenAPI 3.1 schema fragment describing the wire format.

        Used by ``lib/openapi.py`` so the spec stays in lockstep with the
        actual fields without manual maintenance.
        """
        _PYTYPE_TO_OAS = {
            bool:  {'type': 'boolean'},
            int:   {'type': 'integer'},
            float: {'type': 'number'},
            str:   {'type': 'string'},
            list:  {'type': 'array', 'items': {}},
            dict:  {'type': 'object'},
        }
        properties: dict[str, dict] = {}
        for wire, _attr, typ, default, enum, desc in _FIELDS:
            schema = dict(_PYTYPE_TO_OAS.get(typ, {'type': 'string'}))
            if enum:
                schema['enum'] = list(enum)
            if default is not None and not isinstance(default, list):
                schema['default'] = default
            if desc:
                schema['description'] = desc
            properties[wire] = schema
        return {
            'type': 'object',
            'description': (
                'Typed Tofu agent-run options.  See lib/agent_options.py '
                'for the canonical field list.  Unknown keys are tolerated '
                'and round-trip through the server unchanged.'
            ),
            'additionalProperties': True,
            'properties': properties,
        }

    # ── Convenience ──────────────────────────────────────────────

    def replace(self, **kwargs) -> 'TofuOptions':
        """Return a copy with the given fields overridden.

        Useful for sub-task spawning (e.g. autopilot) that wants to
        override a few flags::

            sub = parent_opts.replace(endpoint_mode=False, autopilot=False)
        """
        existing = {f.name: getattr(self, f.name) for f in fields(self)}
        existing.update(kwargs)
        return type(self)(**existing)


__all__ = ['TofuOptions']
