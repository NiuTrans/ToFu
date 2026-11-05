# HOT_PATH
"""update_search_settings tool handler — thin wrapper over lib.search.search_settings.

The validation/persistence/hot-reload logic for the fetch blocklist lives in
``lib.search.search_settings.apply_updates``. This handler only translates
between the tool-round protocol and that function.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


def _format_effective(eff: dict) -> str:
    """Render the effective blocklist for the tool response."""
    if not isinstance(eff, dict):
        return ''
    skip = eff.get('skip_domains') or []
    return (f"  skip_domains={len(skip)} entries"
            + (f": {', '.join(skip)}" if skip else ''))


@tool_registry.handler('update_search_settings', category='search',
                       description='Read or edit the server-wide fetch blocklist')
def _handle_update_search_settings(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.search import search_settings as ss

    res = ss.apply_updates(fn_args or {})

    parts: list[str] = []
    changed = bool(res.get('applied'))
    if res.get('applied'):
        applied_lines = [f"{key}={', '.join(str(v) for v in val)}"
                         for key, val in res['applied'].items()]
        parts.append('Applied (server-wide, persisted, hot-reloaded):\n  '
                     + '\n  '.join(applied_lines))
    if res.get('errors'):
        parts.append('Rejected:\n  ' + '\n  '.join(
            f'{k}: {v}' for k, v in res['errors'].items()))
    if not changed and not res.get('errors'):
        parts.append('Current fetch blocklist (no changes requested):')
    else:
        parts.append('Fetch blocklist now:')
    parts.append(_format_effective(res.get('effective')))

    tool_content = '\n\n'.join(p for p in parts if p)
    ok = bool(res.get('ok')) and not res.get('errors')
    meta = _build_simple_meta(
        fn_name, tool_content, source='Settings',
        title=('⚙️ Search settings updated' if changed and ok
               else '⚠️ Search settings (partial)' if changed
               else '⚙️ Search settings'),
        snippet=(', '.join(f'{k}={v}' for k, v in (res.get('applied') or {}).items())
                 if changed else 'read current values')[:120],
        badge='✅ applied' if changed and ok else '⚠️ partial' if changed else '👁 read',
        extra={'settingsOk': ok, 'settingsChanged': changed},
    )
    _finalize_tool_round(task, rn, round_entry, [meta],
                         query_override='⚙️ update_search_settings')
    logger.info('[SettingsTool] changed=%s ok=%s applied=%s errors=%s',
                changed, ok, res.get('applied'), res.get('errors') or 'none')
    return tc_id, tool_content, False
