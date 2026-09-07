# HOT_PATH
"""update_search_settings tool handler — thin wrapper over lib.search_settings.

The validation/clamping/persistence/hot-reload logic lives in
``lib.search_settings.apply_updates`` (single source of truth, shared with
the Settings UI status projection). This handler only translates between the
tool-round protocol and that function.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.executor import _build_simple_meta, _finalize_tool_round, tool_registry

logger = get_logger(__name__)


def _format_effective(eff: dict) -> str:
    """Render the effective-config snapshot compactly for the tool response."""
    if not isinstance(eff, dict):
        return ''
    skip = eff.get('skip_domains') or []
    lines = [
        f"  fetch_top_n={eff.get('fetch_top_n')}",
        f"  fetch_timeout={eff.get('fetch_timeout')}s",
        f"  max_chars_search={eff.get('max_chars_search')}",
        f"  max_chars_direct={eff.get('max_chars_direct')}",
        f"  max_chars_pdf={eff.get('max_chars_pdf')}",
        f"  max_bytes={eff.get('max_bytes')} "
        f"(~{round((eff.get('max_bytes') or 0) / 1048576, 1)} MB)",
        f"  llm_content_filter={'on' if eff.get('llm_content_filter') else 'off'}",
        f"  skip_domains={len(skip)} entries"
        + (f": {', '.join(skip[:10])}{' …' if len(skip) > 10 else ''}" if skip else ''),
    ]
    return '\n'.join(lines)


@tool_registry.handler('update_search_settings', category='search',
                       description='Read or adjust server-wide search/fetch settings')
def _handle_update_search_settings(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib import search_settings as ss

    res = ss.apply_updates(fn_args or {})

    parts: list[str] = []
    changed = bool(res.get('applied'))
    if res.get('applied'):
        applied_lines = []
        for key, val in res['applied'].items():
            if key == 'max_bytes':
                applied_lines.append(f"max_download_mb={round(val / 1048576, 1)} (max_bytes={val})")
            elif isinstance(val, list):
                applied_lines.append(f"{key}={', '.join(str(v) for v in val)}")
            else:
                applied_lines.append(f"{key}={val}")
        parts.append('Applied (server-wide, persisted, hot-reloaded):\n  '
                     + '\n  '.join(applied_lines))
    if res.get('errors'):
        parts.append('Rejected:\n  ' + '\n  '.join(
            f'{k}: {v}' for k, v in res['errors'].items()))
    for note in res.get('notes') or []:
        parts.append(f'NOTE: {note}')
    if not changed and not res.get('errors'):
        parts.append('Current effective search/fetch settings (no changes requested):')
    else:
        parts.append('Effective settings now:')
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
    return tc_id, tool_content, ok
