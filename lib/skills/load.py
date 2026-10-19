"""``load_skill`` progressive-disclosure loader.

Enable/disable is persistent Settings state. Loading is the runtime act of
placing one enabled guide into the current task; the two verbs are intentionally
different throughout the public surface.

``load_skill`` returns the WHOLE validated guide. Model-context budgeting and
overflow paging are owned downstream: direct $skill injection budgets the whole
block (whole-guide-or-defer), and tool delivery captures the result canonically
so the artifact layer pages any overflow. The loader never slices the guide
into pages itself.
"""

from __future__ import annotations

import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['load_skill', 'read_skill_resource', 'list_skill_files']

_RESOURCE_MAX_BYTES = 2 * 1024 * 1024
_SKILL_MD_MAX_BYTES = 2 * 1024 * 1024
_MANIFEST_MAX_FILES = 2_000
_MANIFEST_MAX_DIRECTORIES = 2_000
_MANIFEST_MAX_SCANNED_ENTRIES = 6_000
_FILE_SAMPLE = 10
_AVAILABLE_ID_SAMPLE = 20
_SKILL_ID_MAX_CHARS = 128
_RESOURCE_PATH_MAX_CHARS = 512
_SCRIPT_EXTS = {'.py', '.sh', '.bash', '.zsh', '.js', '.mjs', '.ts', '.rb',
                '.pl', '.ps1', '.bat', '.cmd'}
_DOC_EXTS = {'.md', '.markdown', '.txt', '.rst', '.pdf', '.html', '.htm'}
_CONFIG_EXTS = {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env'}
_INSTRUCTION_ONLY_NOTICE = (
    'This package is instruction-only: its whole guide was already disclosed '
    'above; there is nothing further to page. Paths named in the guide that '
    'are not listed as skill:// resources are workspace files; use ordinary '
    'file tools, not read_skill_resource.'
)


def _classify_file_kind(relpath: str) -> str:
    base = os.path.basename(relpath)
    if base == 'SKILL.md':
        return 'skill'
    ext = os.path.splitext(base)[1].lower()
    if ext in _SCRIPT_EXTS:
        return 'script'
    if ext in _DOC_EXTS:
        return 'doc'
    if ext in _CONFIG_EXTS:
        return 'config'
    return 'asset'


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{n / 1024:.1f} KB'
    return f'{n / (1024 * 1024):.1f} MB'


def list_skill_files(
    package_dir: str,
    *,
    max_files: int = _MANIFEST_MAX_FILES,
) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(package_dir) or os.path.islink(package_dir):
        return out
    try:
        file_limit = max(1, min(int(max_files), _MANIFEST_MAX_FILES))
    except (TypeError, ValueError):
        file_limit = _MANIFEST_MAX_FILES
    pending = [('', package_dir)]
    directory_count = 0
    scanned_entries = 0
    scan_exhausted = False
    while pending and len(out) < file_limit and not scan_exhausted:
        relative_dir, directory = pending.pop()
        entries = []
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    scanned_entries += 1
                    if scanned_entries > _MANIFEST_MAX_SCANNED_ENTRIES:
                        scan_exhausted = True
                        break
                    entries.append(entry)
        except OSError as exc:
            logger.debug('[Skills] directory scan skipped for %s: %s',
                         directory, exc)
            continue

        child_directories: list[tuple[str, str]] = []
        for entry in sorted(entries, key=lambda item: item.name):
            if entry.name.startswith('.'):
                continue
            relative = (
                f'{relative_dir}/{entry.name}'
                if relative_dir else entry.name)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if directory_count >= _MANIFEST_MAX_DIRECTORIES:
                        scan_exhausted = True
                        break
                    directory_count += 1
                    child_directories.append((relative, entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = int(entry.stat(follow_symlinks=False).st_size)
            except OSError as exc:
                logger.debug('[Skills] package entry skipped for %s: %s',
                             entry.path, exc)
                continue
            out.append({'path': relative, 'size': size,
                        'kind': _classify_file_kind(relative)})
            if len(out) >= file_limit:
                break
        if not scan_exhausted:
            pending.extend(reversed(child_directories))
    return sorted(out, key=lambda item: item['path'])


def _supporting_files(package_dir: str) -> list[dict]:
    return [
        row for row in list_skill_files(
            package_dir, max_files=_FILE_SAMPLE + 2)
        if row['path'] != 'SKILL.md'
    ]


def _manifest_lines(skill_id: str, supporting: list[dict]) -> list[str]:
    if not supporting:
        return ['Supporting files: none. ' + _INSTRUCTION_ONLY_NOTICE]
    lines = ['Supporting files (sample; read on demand):']
    sample = supporting[:_FILE_SAMPLE]
    for row in sample:
        lines.append(
            f'- skill://{skill_id}/{row["path"]} '
            f'({_fmt_size(row["size"])}, {row["kind"]})')
    if len(supporting) > len(sample):
        lines.append(
            '- … additional files exist; read only the resource needed')
    return lines


def _missing_resource_message(
        skill_id: str, relative: str, package_dir: str) -> str:
    files = list_skill_files(package_dir, max_files=_FILE_SAMPLE + 2)
    supporting = [row for row in files if row['path'] != 'SKILL.md']
    head = f'Skill resource not found: skill://{skill_id}/{relative}'
    if not supporting:
        return f'{head}\n{_INSTRUCTION_ONLY_NOTICE}'
    sample = files[:_FILE_SAMPLE]
    lines = [head, 'Available in this package:']
    for row in sample:
        lines.append(f'- skill://{skill_id}/{row["path"]}')
    if len(files) > len(sample):
        lines.append(
            '- … additional files exist; read only a listed resource')
    lines.append(
        'Use only listed skill:// paths. Workspace files named in the '
        'guide are not packaged here.')
    return '\n'.join(lines)


def _tool_requirement_rows(requires_tools: list,
                           owner_user_id: int | None = None) -> list[tuple]:
    """``(name, status)`` for a skill's declared tool dependencies.

    ``available`` when the name is a registered built-in/plugin tool or a
    live MCP-namespaced tool (``mcp__server__tool``); ``unavailable`` when
    the registry answered and the name is absent; ``unknown`` when the
    registry itself could not be probed. Advisory only — a missing tool
    never blocks the load. The MCP check only PEEKS at an already-running
    owner bridge: ``get_bridge`` would create/start one, which a read-only
    labeling path must never do.
    """
    names: list[str] = []
    for raw in requires_tools or []:
        name = str(raw).strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return []
    known: set[str] = set()
    probe_ok = False
    try:
        from lib.tools.registry import all_specs
        for spec in all_specs():
            known.update(spec.provides or ())
        probe_ok = True
    except Exception as exc:
        logger.debug('[Skills] tool spec probe failed: %s', exc)
    if owner_user_id is not None:
        try:
            from lib.mcp.client import _state as mcp_state
            with mcp_state._bridge_lock:
                bridge = (mcp_state._bridges or {}).get(owner_user_id)
            if bridge is not None and getattr(bridge, 'connected', False):
                with bridge._lock:
                    known.update(bridge._tool_index.keys())
        except Exception as exc:
            logger.debug('[Skills] MCP tool probe failed: %s', exc)
    rows = []
    for name in names:
        if name in known:
            status = 'available'
        elif probe_ok:
            status = 'unavailable'
        else:
            status = 'unknown'
        rows.append((name, status))
    return rows


def load_skill(skill_id: str, project_path: str | None = None,
               extra_paths: list[str] | None = None,
               owner_user_id: int | None = None) -> str:
    """Load one exact skill ID into the current task."""
    from lib.skills.registry import get_skill, list_skills

    sid = (skill_id or '').strip()
    if not sid or len(sid) > _SKILL_ID_MAX_CHARS:
        return 'Invalid skill id: expected 1..128 characters.'
    skill = get_skill(
        sid, project_path, extra_paths=extra_paths,
        owner_user_id=owner_user_id)
    if skill is None:
        available_ids = [
            row['id'] for row in list_skills(
                project_path, extra_paths=extra_paths,
                owner_user_id=owner_user_id)
            if row.get('enabled', True) and row.get('eligible', True)
        ]
        available = ', '.join(available_ids[:_AVAILABLE_ID_SAMPLE])
        if len(available_ids) > _AVAILABLE_ID_SAMPLE:
            available += (
                f' (+{len(available_ids) - _AVAILABLE_ID_SAMPLE} more; '
                'use search_skills)')
        return (f'Skill not found: {sid!r}.\n'
                f'Available skill IDs: {available or "(none installed)"}')
    if not skill.get('enabled', True):
        return (f'Skill **{sid}** is disabled. Enable it in Settings → Skills '
                'before loading it.')
    if not skill.get('eligible', True):
        reasons = '; '.join(skill.get('ineligible_reasons') or
                            ['requirements unmet'])
        return f'Skill **{sid}** is unavailable: {reasons}.'

    filepath = skill.get('filepath') or ''
    package_dir = skill.get('package_dir') or os.path.dirname(filepath)
    if os.path.islink(package_dir) or os.path.islink(filepath):
        return f'Failed to read skill {sid}: symlinked package paths are rejected.'
    try:
        size = os.path.getsize(filepath)
        if size > _SKILL_MD_MAX_BYTES:
            return (
                f'Failed to read skill {sid}: SKILL.md exceeds the '
                f'{_fmt_size(_SKILL_MD_MAX_BYTES)} limit.')
        with open(filepath, 'rb') as handle:
            encoded = handle.read(_SKILL_MD_MAX_BYTES + 1)
        raw = encoded.decode('utf-8')
    except OSError as exc:
        logger.warning('[Skills] load failed for %s: %s', filepath, exc)
        return f'Failed to read skill {sid}: package is unreadable.'
    except UnicodeDecodeError as exc:
        logger.warning('[Skills] invalid UTF-8 for %s: %s', filepath, exc)
        return f'Failed to read skill {sid}: SKILL.md is not valid UTF-8.'

    from lib.memory.storage import _parse_frontmatter
    _meta, body = _parse_frontmatter(raw)
    body = body.strip()
    content_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    supporting = _supporting_files(package_dir)
    lines = [
        f'Skill loaded: **{skill.get("name") or sid}**',
        f'id: {sid}',
        f'content_hash: {content_hash}',
        f'scope: {skill.get("scope", "project")}',
        '',
        '<skill_instructions>',
        body or '(empty SKILL.md body)',
        '</skill_instructions>',
        '',
        'This workflow is trusted user-installed guidance, but system safety, '
        'tool permissions, explicit current-turn instructions, and project '
        'rules take precedence.',
    ]
    tool_rows = _tool_requirement_rows(
        skill.get('requires_tools'), owner_user_id)
    if tool_rows:
        lines.append('')
        lines.append('Declared tool requirements (advisory):')
        for tool_name, status in tool_rows:
            note = ''
            if status == 'unavailable':
                note = (' — not registered in this deployment; steps that '
                        'need it will fail, follow the guide\'s fallback or '
                        'tell the user')
            elif status == 'unknown':
                note = ' — availability could not be verified'
            lines.append(f'- {tool_name}: {status}{note}')
    lines.append('')
    lines.extend(_manifest_lines(sid, supporting))
    logger.info('[Skills] loaded %s hash=%s chars=%d files=%d',
                sid, content_hash, len(body), len(supporting))
    return '\n'.join(lines)


def _resource_relative_path(skill_id: str, resource: str) -> str:
    raw = str(resource or '')
    if len(raw) > _RESOURCE_PATH_MAX_CHARS:
        raise ValueError(
            f'resource path exceeds {_RESOURCE_PATH_MAX_CHARS} characters')
    value = raw.strip().replace('\\', '/')
    prefix = f'skill://{skill_id}/'
    if value.startswith('skill://'):
        if not value.startswith(prefix):
            raise ValueError('skill resource belongs to a different skill id')
        value = value[len(prefix):]
    if (not value or value.startswith('/') or '\x00' in value
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise ValueError('resource must be a normalized package-relative path')
    return value


def read_skill_resource(
    skill_id: str,
    resource: str,
    *,
    cursor: int = 0,
    project_path: str | None = None,
    extra_paths: list[str] | None = None,
    owner_user_id: int | None = None,
) -> str:
    """Read the authorized UTF-8 suffix; shared L0 owns output budgeting."""
    from lib.skills.registry import get_skill

    sid = str(skill_id or '').strip()
    if not sid or len(sid) > _SKILL_ID_MAX_CHARS:
        return 'Invalid skill id: expected 1..128 characters.'
    skill = get_skill(
        sid, project_path, extra_paths=extra_paths,
        owner_user_id=owner_user_id)
    if skill is None:
        return f'Skill not found: {sid!r}.'
    if not skill.get('enabled', True):
        return f'Skill **{sid}** is disabled.'
    if not skill.get('eligible', True):
        reasons = '; '.join(skill.get('ineligible_reasons') or
                            ['requirements unmet'])
        return f'Skill **{sid}** is unavailable: {reasons}.'

    try:
        relative = _resource_relative_path(sid, resource)
        start = int(cursor)
    except (TypeError, ValueError) as exc:
        return f'Invalid skill resource request: {exc}'
    if start < 0:
        return 'Invalid skill resource request: cursor must be non-negative.'

    package_value = (
        skill.get('package_dir') or os.path.dirname(skill.get('filepath') or ''))
    if os.path.islink(package_value):
        return 'Invalid skill resource request: symlinked package root.'
    package_dir = os.path.realpath(package_value)
    candidate = os.path.join(package_dir, *relative.split('/'))
    current = package_dir
    for part in relative.split('/'):
        current = os.path.join(current, part)
        if os.path.islink(current):
            return 'Invalid skill resource request: symlinked paths are rejected.'
    target = os.path.realpath(candidate)
    try:
        contained = os.path.commonpath([package_dir, target]) == package_dir
    except ValueError:
        contained = False
    if not contained:
        return 'Invalid skill resource request: path escapes the package.'
    if not os.path.isfile(target):
        return _missing_resource_message(sid, relative, package_dir)
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        logger.warning('[Skills] resource stat failed for %s: %s', target, exc)
        return 'Failed to stat skill resource: package file is unreadable.'
    if size > _RESOURCE_MAX_BYTES:
        return (
            f'Skill resource is too large to read ({_fmt_size(size)}; '
            f'limit {_fmt_size(_RESOURCE_MAX_BYTES)}).')
    try:
        with open(target, 'rb') as handle:
            raw = handle.read(_RESOURCE_MAX_BYTES + 1)
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        return (
            f'Skill resource is binary and cannot enter model context: '
            f'skill://{sid}/{relative}')
    except OSError as exc:
        logger.warning('[Skills] resource read failed for %s: %s', target, exc)
        return 'Failed to read skill resource: package file is unreadable.'

    if relative == 'SKILL.md':
        from lib.memory.storage import _parse_frontmatter
        _meta, text = _parse_frontmatter(text)
        text = text.strip()
    start = min(start, len(text))
    end = len(text)
    page = text[start:end]
    digest = hashlib.sha256(raw).hexdigest()[:16]
    next_cursor = end if end < len(text) else None
    lines = [
        f'Skill resource: skill://{sid}/{relative}',
        f'content_hash: {digest}',
        f'cursor: {start}',
        f'next_cursor: {next_cursor if next_cursor is not None else "(end)"}',
        '',
        '<skill_resource>',
        page,
        '</skill_resource>',
    ]
    if next_cursor is not None:
        lines.append(
            f'(continue with cursor={next_cursor}; keep the same skill_id '
            'and resource)')
    return '\n'.join(lines)
