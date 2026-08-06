"""lib/tools/registry/_introspect.py — Live tool-registry inventory.

The **read side** of the declarative registry: walks ``_TOOL_SPECS`` (the
single home in :mod:`._spec`) and produces a human-consumable inventory of
every tool family registered in this process — grouped by category, with
each family's gate evaluated against a **reference context** (the server's
current defaults: search on, no project attached, compositional features
matching what a plain chat turn would see) so the answer to *"would this
family be offered to the model right now?"* is computed by the spec's own
``build()`` — never by a hand-mirrored guess.

Born 2026-08-06 (owner directive: a Settings → 工具 panel must show every
tool registered in real time — "I don't even know what tools the project
grew"). It exists because the pre-existing ``capabilities._tools_summary``
hand-listed 5 groups and had silently drifted from the real registry
(memory/scheduler/swarm/motion/conv-ref/… all missing).

What it deliberately is NOT
---------------------------
* **Not an execution surface.** Building schemas for introspection runs the
  same lazy imports the request path runs; handlers are never invoked.
* **Not per-request truth.** Gates like conv-ref (needs an @-mention) or
  plugin allow-lists are per-request; the inventory answers for the
  reference context and says so per family via ``gate`` / ``gate_state``.
* **Not cached.** The payload is small and the panel is opened by a human;
  computing fresh on every GET keeps the panel REAL (an MCP reconnect or a
  plugin install shows up on the next open).

MCP tools are attached from the live bridge (when started): ``_tool_index``
is enumerated so a user-disabled tool shows as ``off`` instead of
vanishing — the panel must show everything *registered*, with state, not
only what the model currently sees.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

from lib.tools.registry._spec import ToolContext, all_specs

logger = get_logger(__name__)


def _reference_context() -> ToolContext:
    """The reference gate context — mirrors a plain chat turn's defaults.

    ``search_mode`` follows the same default as
    ``lib/tasks_pkg/model_config._resolve_model_config`` (``multi``).
    Feature toggles that live in the persisted server config
    (browser/swarm/image_gen/human_guidance/mcp) are read from
    ``lib._SAVED_CONFIG['features']`` when present so the panel reflects
    the operator's actual switches; absent keys fall back to the chat
    defaults (mcp on, the rest off — the same fail-closed posture as a
    fresh deployment).
    """
    feats: dict[str, Any] = {}
    try:
        import lib as _lib  # type: ignore
        saved = getattr(_lib, '_SAVED_CONFIG', None) or {}
        raw = saved.get('features')
        if isinstance(raw, dict):
            feats = raw
    except Exception as e:
        logger.debug('[ToolInventory] saved config unavailable: %s', e)

    return ToolContext(
        cfg={},
        task_id='tool-inventory',
        project_path='',
        project_enabled=False,
        search_mode='multi',
        search_enabled=True,
        fetch_enabled=True,
        code_exec_enabled=False,
        browser_enabled=bool(feats.get('browserEnabled', False)),
        desktop_enabled=bool(feats.get('desktopEnabled', False)),
        swarm_enabled=bool(feats.get('swarmEnabled', False)),
        image_gen_enabled=bool(feats.get('imageGenEnabled', False)),
        human_guidance_enabled=bool(feats.get('humanGuidanceEnabled', False)),
        scheduler_enabled=True,
        messages=[],
        # The reference context sees no third-party plugins (the multi-tenant
        # fail-closed default). Plugin specs still appear in the inventory —
        # evaluated as hidden — because the panel's job is to show what is
        # REGISTERED, including what's currently invisible to requests.
        enabled_plugins=set(),
    )


def _schema_name(schema: dict) -> str:
    fn = schema.get('function') if isinstance(schema, dict) else None
    if isinstance(fn, dict) and fn.get('name'):
        return str(fn['name'])
    return str(schema.get('name') or '') if isinstance(schema, dict) else ''


def _schema_description(schema: dict) -> str:
    fn = schema.get('function') if isinstance(schema, dict) else None
    if isinstance(fn, dict):
        return str(fn.get('description') or '')
    return str(schema.get('description') or '') if isinstance(schema, dict) else ''


def _required_params(schema: dict) -> list[str]:
    fn = schema.get('function') if isinstance(schema, dict) else None
    params = (fn or {}).get('parameters') if isinstance(fn or {}, dict) else None
    if isinstance(params, dict) and isinstance(params.get('required'), list):
        return [str(p) for p in params['required']]
    return []


def _handler_coverage() -> tuple[set[str], set[str]]:
    """(handler-bound names, write-partition names) from the live dispatch
    registry + the spec unions. Any failure (executor not imported yet —
    e.g. a very early call) degrades to empty sets: the panel then simply
    omits handler/write badges rather than failing the whole inventory."""
    bound: set[str] = set()
    writes: set[str] = set()
    try:
        import lib.tasks_pkg.handlers  # noqa: F401 — ensure decorators ran
        from lib.tasks_pkg.executor import tool_registry
        bound = {name for name, _c, _d in tool_registry.list_tools()}
    except Exception as e:
        logger.debug('[ToolInventory] dispatch registry unavailable: %s', e)
    try:
        from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
        writes = set(_WRITE_TOOLS)
    except Exception as e:
        logger.debug('[ToolInventory] write partition unavailable: %s', e)
    return bound, writes


def _mcp_rows() -> list[dict]:
    """Every MCP tool the live bridge has discovered (connected servers).

    Reads ``_tool_index`` directly so per-server user-disabled tools are
    listed with ``enabled: False`` (they're hidden from the model but still
    registered — the panel shows state, not just the model-visible subset).
    """
    rows: list[dict] = []
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if not bridge or not bridge.connected:
            return []
        safety: dict[str, bool] = {}
        try:
            safety = bridge.get_tool_safety()
        except Exception as e:
            logger.debug('[ToolInventory] mcp safety map failed: %s', e)
        with bridge._lock:
            index_items = sorted(bridge._tool_index.items())
            for ns, info in index_items:
                server = info.get('server_name', '')
                tool_name = info.get('tool_name', '')
                disabled = tool_name in bridge._disabled_tools_for(server)
                odef = info.get('openai_def') or {}
                rows.append({
                    'name': ns,
                    'description': _schema_description(odef)[:300],
                    'required': _required_params(odef),
                    'write': not safety.get(ns, False),
                    'handler': True,
                    'enabled': not disabled,
                    'server': server,
                })
    except Exception as e:
        logger.debug('[ToolInventory] MCP enumeration failed: %s', e)
    return rows


def _mcp_servers() -> list[dict]:
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if not bridge:
            return []
        return bridge.list_servers()
    except Exception as e:
        logger.debug('[ToolInventory] MCP server list failed: %s', e)
        return []


def build_tool_inventory() -> dict:
    """Assemble the full live inventory. Never raises — every enrichment is
    independently fail-soft so the panel always gets a renderable payload.

    Returns:
        ``{generated_at, reference, totals, groups}`` where ``groups`` is a
        list of ``{id, families: [{key, phase, source, plugin_name,
        description, gate, gate_state, builtin_tools, mcp_tools, plugins}]}``.
    """
    import time

    ctx = _reference_context()
    bound, writes = _handler_coverage()

    # ── Assemble per-spec; plugin allow-list resolution for display ──
    try:
        from lib.tools.registry._plugins import available_plugins
        installed_plugins = available_plugins()
    except Exception as e:
        logger.debug('[ToolInventory] plugin list failed: %s', e)
        installed_plugins = []

    groups: dict[str, dict] = {}

    def _group(gid: str) -> dict:
        g = groups.get(gid)
        if g is None:
            g = {'id': gid, 'families': []}
            groups[gid] = g
        return g

    total_tools = 0
    active_tools = 0

    # ── Two-phase evaluation, mirroring assemble_tool_list exactly ──
    # Capability-phase specs self-gate on ctx.has_base_tools / current_count,
    # which the assembler mutates BETWEEN phases. Evaluating each spec in
    # isolation with a pristine ctx would wrongly report memory/scheduler/
    # skills/todo as gated-off (they attach whenever any base tool exists).
    # So we walk the phases in order, threading the same ctx mutations.
    contributions: dict[str, list[dict]] = {}
    build_errors: dict[str, str] = {}
    running_count = 0
    for phase in ('base', 'capability'):
        for spec in all_specs():
            if spec.phase != phase:
                continue
            if spec.source == 'plugin' and not ctx.plugin_allowed(spec.plugin_name):
                continue  # reported as plugin_not_allowlisted below
            ctx.current_count = running_count
            try:
                schemas = spec.build(ctx) or []
            except Exception as e:
                logger.warning('[ToolInventory] spec %s build failed during '
                               'inventory: %s', spec.key, e)
                schemas = []
                build_errors[spec.key] = str(e)
            contributions[spec.key] = schemas
            running_count += len(schemas)
        if phase == 'base':
            ctx.has_base_tools = running_count > 0

    for spec in all_specs():
        gid = spec.category or 'other'
        family = {
            'key': spec.key,
            'phase': spec.phase,
            'source': spec.source,
            'plugin_name': spec.plugin_name,
            'description': spec.description,
            'gate': getattr(spec, 'gate', ''),
            'declared': sorted(spec.provides),
        }
        tools: list[dict] = []
        gate_state = 'on'
        gate_reason = ''
        if spec.source == 'plugin' and not ctx.plugin_allowed(spec.plugin_name):
            gate_state = 'off'
            gate_reason = 'plugin_not_allowlisted'
        else:
            if spec.key in build_errors:
                gate_state = 'error'
                gate_reason = f'build_error: {build_errors[spec.key]}'
            for schema in contributions.get(spec.key, []):
                if not isinstance(schema, dict):
                    continue
                name = _schema_name(schema)
                if not name:
                    continue
                tools.append({
                    'name': name,
                    'description': _schema_description(schema)[:300],
                    'required': _required_params(schema),
                    'write': name in writes or name in spec.write_tools,
                    'handler': name in bound or spec.handler is not None,
                    'enabled': True,
                })
            if gate_state == 'on' and not tools:
                gate_state = 'off'
                gate_reason = 'gate_closed'
        # Declared-but-currently-not-contributed names (provides minus built)
        # are listed as disabled rows so the family still shows its full
        # surface (e.g. browser family with the extension disconnected).
        built_names = {t['name'] for t in tools}
        for name in sorted(spec.provides - built_names):
            tools.append({
                'name': name,
                'description': '',
                'required': [],
                'write': name in writes or name in spec.write_tools,
                'handler': name in bound,
                'enabled': False,
            })
        family['gate_state'] = gate_state
        family['gate_reason'] = gate_reason
        family['tools'] = tools
        family['mcp_tools'] = []
        if spec.key == 'mcp':
            family['mcp_tools'] = _mcp_rows()
            family['mcp_servers'] = _mcp_servers()
            if family['mcp_tools'] and gate_state == 'off':
                # cfg default mcpEnabled=True but bridge disconnected —
                # distinguish "off by config" from "on but no server".
                gate_state = 'standby'
                gate_reason = 'no_server_connected'
                family['gate_state'] = gate_state
                family['gate_reason'] = gate_reason
        n_active = sum(1 for t in tools if t['enabled']) + \
            sum(1 for t in family['mcp_tools'] if t['enabled'])
        n_total = len(tools) + len(family['mcp_tools'])
        family['counts'] = {'active': n_active, 'total': n_total}
        total_tools += n_total
        active_tools += n_active
        _group(gid)['families'].append(family)

    # Plugin specs carry source='plugin'; surface which entry points are
    # installed at all so a registered-but-absent plugin is explainable.
    inventory = {
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'reference': {
            'search_mode': ctx.search_mode,
            'project_attached': bool(ctx.project_enabled),
            'browser_enabled': ctx.browser_enabled,
            'desktop_enabled': ctx.desktop_enabled,
            'swarm_enabled': ctx.swarm_enabled,
            'image_gen_enabled': ctx.image_gen_enabled,
            'human_guidance_enabled': ctx.human_guidance_enabled,
            'mcp_enabled': ctx.cfg.get('mcpEnabled', True),
            'note': 'Gates evaluated against server defaults for a plain '
                    'chat turn (no project attached). Per-request states '
                    '(conv-ref @-mention, per-request plugin allow-lists, '
                    'project tools) are shown as registered-but-gated.',
        },
        'installed_plugins': installed_plugins,
        'totals': {
            'families': sum(len(g['families']) for g in groups.values()),
            'tools': total_tools,
            'active': active_tools,
        },
        'groups': [groups[k] for k in sorted(groups.keys())],
    }
    return inventory


__all__ = ['build_tool_inventory']
