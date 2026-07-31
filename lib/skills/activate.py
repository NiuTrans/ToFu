"""lib/skills/activate.py — ``activate_skill`` progressive-disclosure loader.

Given a skill id (or name, as listed in ``<available_skills>``), load the
full SKILL.md guide plus a manifest of the package's bundled files
(``references/`` docs, ``scripts/``, assets) with absolute paths so the
model can read them on demand with ``read_files``. This completes the
progressive-disclosure loop: index (every turn) → activate (on match) →
bundled files (on demand).

Honest failures: an unknown skill answers with the list of available ids so
the model can self-correct; a disabled or ineligible skill reports WHY
without leaking its instructions.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['activate_skill', 'list_skill_files']

_BODY_CAP = 100_000  # chars — defensive; eligible SKILL.md guides are small

_SCRIPT_EXTS = {'.py', '.sh', '.bash', '.zsh', '.js', '.mjs', '.ts', '.rb',
                '.pl', '.ps1', '.bat', '.cmd'}
_DOC_EXTS = {'.md', '.markdown', '.txt', '.rst', '.pdf', '.html', '.htm'}
_CONFIG_EXTS = {'.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.env'}


def _classify_file_kind(relpath: str) -> str:
    """Classify a package file for the manifest: skill / doc / script /
    config / asset (mirrors the Settings file-browser kinds)."""
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
    """Recursively enumerate a skill package's files.

    Returns ``[{path, size, kind}]`` sorted by relative path (``path`` is
    relative to ``package_dir``; ``kind`` from :func:`_classify_file_kind`).
    Shared by :func:`activate_skill` (model-facing manifest) and the
    Settings API file browser.
    """
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
            except OSError as e:
                logger.debug('[Skills] getsize failed for %s: %s', full, e)
                size = 0
            out.append({'path': rel, 'size': size,
                        'kind': _classify_file_kind(rel)})
    out.sort(key=lambda f: f['path'])
    return out


def _resolve_skill(query: str, project_path, extra_paths):
    """Resolve a skill by id first, then by (case-insensitive) name.

    Returns ``(skill_dict | None, all_skills)``.
    """
    from lib.skills.registry import list_skills

    skills = list_skills(project_path, extra_paths=extra_paths)
    q = (query or '').strip()
    for s in skills:
        if s['id'] == q:
            return s, skills
    ql = q.lower()
    for s in skills:
        if (s.get('name') or '').strip().lower() == ql:
            return s, skills
    return None, skills


def activate_skill(query: str,
                   project_path: str | None = None,
                   extra_paths: list[str] | None = None) -> str:
    """Load a skill's full instructions + bundled-file manifest.

    Args:
        query: Skill id (preferred) or display name, as listed in
            ``<available_skills>``.
        project_path: Workspace primary root (skills may live under
            ``<root>/.tofu/skills/``).
        extra_paths: Extra workspace roots (multi-root sessions).

    Returns:
        Model-facing text: the SKILL.md body plus a file manifest, or an
        actionable error string (unknown / disabled / ineligible).
    """
    skill, all_skills = _resolve_skill(query, project_path, extra_paths)
    if skill is None:
        available = ', '.join(
            f"{s['id']} ({s.get('scope', 'project')})"
            for s in sorted(all_skills, key=lambda m: m['id'])
            if s.get('enabled', True) and s.get('eligible', True))
        logger.info('[Skills] activate_skill: unknown skill %r', query)
        return (f"Skill not found: {query!r}.\n"
                f"Available skills: {available or '(none installed)'}")

    sid = skill['id']
    if not skill.get('enabled', True):
        logger.info('[Skills] activate_skill: %s is disabled', sid)
        return (f"Skill **{sid}** is currently DISABLED. It can be "
                f"re-enabled in the Settings → Skills tab; its instructions "
                f"are not loaded while disabled.")
    if not skill.get('eligible', True):
        reasons = '; '.join(skill.get('ineligible_reasons') or
                            ['requirements unmet'])
        logger.info('[Skills] activate_skill: %s ineligible: %s', sid, reasons)
        return (f"Skill **{sid}** cannot be used on this machine: {reasons}. "
                f"Install the missing requirements (or ask the user to) and "
                f"try again.")

    filepath = skill.get('filepath') or ''
    package_dir = skill.get('package_dir') or os.path.dirname(filepath)
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError as e:
        logger.warning('[Skills] activate_skill: read failed for %s: %s',
                       filepath, e)
        return f"Failed to read skill {sid}: {e}"

    from lib.memory.storage import _parse_frontmatter
    _meta, body = _parse_frontmatter(raw)
    body = body.strip()
    truncated = False
    if len(body) > _BODY_CAP:
        body = body[:_BODY_CAP]
        truncated = True

    files = list_skill_files(package_dir)
    manifest_lines = []
    for f in files:
        if f['path'] == 'SKILL.md':
            note = 'the skill entry point (already loaded above)'
        elif f['kind'] == 'script':
            note = 'runnable script'
        elif f['kind'] == 'doc':
            note = 'reference doc'
        elif f['kind'] == 'config':
            note = 'config/data'
        else:
            note = 'asset'
        manifest_lines.append(
            f"- {f['path']} ({_fmt_size(f['size'])}) — {note}")

    parts = [
        f"Skill activated: **{skill.get('name') or sid}** "
        f"(id: {sid}, scope: {skill.get('scope', 'project')})",
        '',
        f"Package location: {os.path.abspath(package_dir)}",
        '',
        '<skill_instructions>',
        body or '(empty SKILL.md body)',
        '</skill_instructions>',
    ]
    if truncated:
        parts.append(f'(instructions truncated at {_BODY_CAP} chars — read '
                     f'the file directly for the rest)')
    if manifest_lines:
        parts += [
            '',
            'Bundled files — read on demand with read_files (paths relative '
            'to the package location above):',
            *manifest_lines,
        ]
    logger.info('[Skills] activated %s (%d chars, %d bundled files)',
                sid, len(body), len(files))
    return '\n'.join(parts)
