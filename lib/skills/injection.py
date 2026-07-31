"""lib/skills/injection.py — The always-visible ``<available_skills>`` index.

This is the skills channel's discovery seam: a compact, byte-stable index of
every installed skill package spliced into the system prompt on EVERY
tool-bearing turn (by ``lib/tasks_pkg/system_context/_inject.py``), so the
model always knows which guides exist and can pull one in on demand via the
``activate_skill`` tool (progressive disclosure — only the one-line
descriptions ride every turn; the full guide is loaded on activation).

Byte-stability contract (prompt-cache safety, same rule as the memory count
hint): the block is a pure function of the installed-skill set — skills are
sorted by id, whitespace in descriptions is collapsed, and the block is empty
(no splice) when nothing is installed. It changes ONLY on user-driven
install / uninstall / enable-toggle, never on model-side CRUD (the model has
no skill CRUD), so it can never invalidate the prompt cache mid-turn.
"""

from __future__ import annotations

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_skills_index']

_DESC_CAP = 300
_WS_RE = re.compile(r'\s+')


def _one_line(text: str) -> str:
    """Collapse a (possibly multi-line) description to a single line."""
    line = _WS_RE.sub(' ', (text or '')).strip()
    if len(line) > _DESC_CAP:
        line = line[:_DESC_CAP - 1].rstrip() + '…'
    return line


def build_skills_index(project_path: str | None = None,
                       extra_paths: list[str] | None = None) -> str:
    """Build the ``<available_skills>`` system-prompt block.

    Lists every installed, ENABLED, ELIGIBLE skill package as
    ``- <id> (<scope>): <one-line description>``, sorted by id.

    Returns ``''`` when no usable skill is installed (the caller then
    splices nothing — an empty block would just burn prompt bytes).
    """
    from lib.skills.registry import list_skills

    try:
        skills = list_skills(project_path, extra_paths=extra_paths)
    except Exception as e:
        logger.warning('[Skills] index build failed: %s', e)
        return ''

    visible = [s for s in skills
               if s.get('enabled', True) and s.get('eligible', True)]
    hidden = len(skills) - len(visible)
    if not visible:
        return ''

    lines = [
        '<available_skills>',
        'The USER has installed the following skill packages. A skill is an '
        'instruction GUIDE (not a memory): when the task matches a skill\'s '
        'description, call activate_skill with the skill id to load the full '
        'guide BEFORE doing the task, then follow it. Bundled reference and '
        'script files are listed on activation — read them on demand with '
        'read_files.',
        '',
    ]
    for s in sorted(visible, key=lambda m: m['id']):
        lines.append(f"- {s['id']} ({s.get('scope', 'project')}): "
                     f"{_one_line(s.get('description', ''))}")
    if hidden:
        lines.append(
            f'({hidden} installed skill{"s are" if hidden != 1 else " is"} '
            f'hidden: disabled or requirements unmet — manage them in the '
            f'Settings → Skills tab.)')
    lines.append('</available_skills>')
    return '\n'.join(lines)
