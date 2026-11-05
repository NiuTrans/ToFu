"""Default AGENTS.md for a folder Tofu has just prepared as a managed repository.

render_default_guidance() is pure. It states only facts that managed_repository
observed (layout, shared data, environments) plus Tofu's Git contract, which is
what keeps agents from running Git in a folder whose people never do. The
caller owns the scan, the create-only write and its rollback; the rendered file
is ordinary project guidance that anyone may edit or replace.
"""
from __future__ import annotations

from collections import Counter

_MAX_LAYOUT = 20
_MAX_SHARED = 15
_MAX_READMES = 5
_README_NAMES = frozenset({'readme', 'readme.md', 'readme.txt', 'readme.rst'})


def _code(path: str) -> str:
    text = path.encode('utf-8', 'surrogateescape').decode('utf-8', 'replace')
    text = text.replace('\r', '\\r').replace('\n', '\\n')
    return f'`` {text} ``' if '`' in text else f'`{text}`'


def _plural(count: int, noun: str) -> str:
    return f'{count:,} {noun}' + ('' if count == 1 else 's')


def render_default_guidance(folder_name: str, versioned: list[str], shared: dict[str, str],
                            environments: list[str]) -> str:
    """Render guidance from observed facts; nothing about the project's purpose is guessed."""
    lines = [
        '# Agent guide for this project', '',
        f'Tofu generated this file when it prepared {_code(folder_name)} for isolated project',
        'workers. It is ordinary project guidance: edit or replace it freely. Tofu never',
        'overwrites it.', '',
        '## Version control is handled by Tofu', '',
        '- Tofu created and maintains the Git repository in this folder so project workers run',
        '  in isolated checkouts. Nobody needs to run Git commands here.',
        '- Do not run `git init`, `git commit`, `git checkout`, `git reset`, `git stash`,',
        '  `git clean` or branch commands, and do not edit `.git/`. Tofu snapshots, reviews and',
        "  publishes worker changes itself; a worker's edits reach this folder only after",
        '  publication.',
    ]
    files_per_entry = Counter(path.split('/', 1)[0] for path in versioned if '/' in path)
    top_files = sorted(path for path in versioned if '/' not in path)
    holds_shared = {path.split('/', 1)[0] for path in shared}
    entries = sorted(set(files_per_entry) | holds_shared | set(top_files))
    if entries:
        lines += ['', '## Layout', '']
        for name in entries[:_MAX_LAYOUT]:
            if name in shared:
                lines.append(f'- {_code(name)} — shared data ({shared[name]})')
            elif name in files_per_entry:
                detail = _plural(files_per_entry[name], 'versioned file')
                if name in holds_shared:
                    detail += '; contains shared data'
                lines.append(f'- {_code(name + "/")} — {detail}')
            else:
                lines.append(f'- {_code(name)}')
        if len(entries) > _MAX_LAYOUT:
            lines.append(f'- … and {len(entries) - _MAX_LAYOUT:,} more top-level entries')
    readmes = sorted((path for path in versioned
                      if path.rsplit('/', 1)[-1].lower() in _README_NAMES and path.count('/') <= 2),
                     key=lambda path: (path.count('/'), path))[:_MAX_READMES]
    if readmes:
        lines += ['', 'Start with ' + ', '.join(map(_code, readmes)) + '.']
    if shared:
        lines += ['', '## Shared data', '',
                  'These paths stay in this folder and are not versioned. In worker checkouts they',
                  'are links to this folder:', '']
        for path in sorted(shared)[:_MAX_SHARED]:
            lines.append(f'- {_code(path)} — {shared[path]}')
        if len(shared) > _MAX_SHARED:
            lines.append(f'- … and {len(shared) - _MAX_SHARED:,} more shared paths')
        lines += ['',
                  'Read them in place and treat them as read-only: Tofu file tools refuse writes',
                  'through these links, and a program that writes through one changes this folder',
                  'directly, without review. Project search skips links, so list or read them by',
                  'explicit path. Write new results to a versioned location instead.']
    notes = []
    if '.venv' in environments:
        notes.append('- Python virtual environment `.venv/`: run `.venv/bin/python`. Worker checkouts '
                     'get a private\n  environment that reads its packages; installs there stay local.')
    if 'node_modules' in environments:
        notes.append('- Node dependencies in `node_modules/`: worker checkouts link the installed '
                     'packages.')
    if notes:
        lines += ['', '## Environment', '', *notes]
    return '\n'.join(lines) + '\n'
