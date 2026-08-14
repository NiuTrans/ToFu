"""``load_skill`` progressive-disclosure loader.

Enable/disable is persistent Settings state. Loading is the runtime act of
placing one enabled guide into the current task; the two verbs are intentionally
different throughout the public surface.
"""

from __future__ import annotations

import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['load_skill', 'list_skill_files']

_BODY_CAP = 100_000
_FILE_SAMPLE = 10
_SCRIPT_EXTS = {'.py', '.sh', '.bash', '.zsh', '.js', '.mjs', '.ts', '.rb',
                '.pl', '.ps1', '.bat', '.cmd'}
_DOC_EXTS = {'.md', '.markdown', '.txt', '.rst', '.pdf', '.html', '.htm'}
_CONFIG_EXTS = {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env'}


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


def list_skill_files(package_dir: str) -> list[dict]:
    out: list[dict] = []
    if not os.path.isdir(package_dir):
        return out
    for dirpath, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith('.'))
        for fname in sorted(filenames):
            if fname.startswith('.'):
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, package_dir)
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                logger.debug('[Skills] stat skipped for %s: %s', full, exc)
                size = 0
            out.append({'path': rel, 'size': size,
                        'kind': _classify_file_kind(rel)})
    return sorted(out, key=lambda item: item['path'])


def load_skill(skill_id: str, project_path: str | None = None,
               extra_paths: list[str] | None = None) -> str:
    """Load one exact skill ID into the current task."""
    from lib.skills.registry import get_skill, list_skills

    sid = (skill_id or '').strip()
    skill = get_skill(sid, project_path, extra_paths=extra_paths)
    if skill is None:
        available = ', '.join(
            row['id'] for row in list_skills(
                project_path, extra_paths=extra_paths)
            if row.get('enabled', True) and row.get('eligible', True))
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
    try:
        with open(filepath, 'r', encoding='utf-8') as handle:
            raw = handle.read()
    except OSError as exc:
        logger.warning('[Skills] load failed for %s: %s', filepath, exc)
        return f'Failed to read skill {sid}: {exc}'

    from lib.memory.storage import _parse_frontmatter
    _meta, body = _parse_frontmatter(raw)
    body = body.strip()
    truncated = len(body) > _BODY_CAP
    if truncated:
        body = body[:_BODY_CAP]
    content_hash = hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]
    files = [row for row in list_skill_files(package_dir)
             if row['path'] != 'SKILL.md']
    sample = files[:_FILE_SAMPLE]
    lines = [
        f'Skill loaded: **{skill.get("name") or sid}**',
        f'id: {sid}',
        f'content_hash: {content_hash}',
        f'scope: {skill.get("scope", "project")}',
        f'base_directory: {os.path.abspath(package_dir)}',
        '',
        '<skill_instructions>',
        body or '(empty SKILL.md body)',
        '</skill_instructions>',
        '',
        'This workflow is trusted user-installed guidance, but system safety, '
        'tool permissions, explicit current-turn instructions, and project '
        'rules take precedence.',
    ]
    if truncated:
        lines.append(f'(instructions truncated at {_BODY_CAP} characters)')
    if sample:
        lines.extend(['', 'Supporting files (sample; read on demand):'])
        for row in sample:
            lines.append(
                f'- {os.path.abspath(os.path.join(package_dir, row["path"]))} '
                f'({_fmt_size(row["size"])}, {row["kind"]})')
        if len(files) > len(sample):
            lines.append(f'- … {len(files) - len(sample)} more file(s)')
    logger.info('[Skills] loaded %s hash=%s chars=%d files=%d',
                sid, content_hash, len(body), len(files))
    return '\n'.join(lines)
