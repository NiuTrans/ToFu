"""Enumerate every production site that writes the WHOLE ``conversations.messages`` blob.

Run directly to PRINT the scan surface (charter discipline: verify what a
scanning guard actually sees BEFORE trusting any assertion built on it)::

    python tests/_scan_conv_messages_writers.py

The guard ``tests/test_conv_messages_cas_writes.py`` imports :func:`scan_writers`
so the ratchet and this human-readable dump can never disagree about the input
set.

WHY THIS EXISTS
---------------
``conversations.messages`` is a single JSON blob holding the whole transcript.
Every writer therefore does read-modify-write. The row carries a ``rev`` column
bumped by a DB trigger on every genuine messages change, so a writer CAN make
its write conditional (``WHERE ... AND rev=?``) and lose the race safely. A
writer that omits that predicate silently overwrites rows another thread
appended between its read and its write — with no error and no red test.

Measured incident (conv ms3sfyrmn31omb, 2026-07-28): 13 ``Appended VU msg`` log
lines, 8 surviving ``_isVirtualUser`` rows. Five autopilot turns were erased by
concurrent blob writers holding a stale copy.

DETECTION
---------
Pure AST, no regex on source text: walk every ``ast.Constant`` string in the
file and keep the ones whose SQL both (a) updates ``conversations`` and (b)
assigns the ``messages`` column. CAS is then decided by whether the same SQL
string constrains ``rev`` or ``updated_at`` in its WHERE clause.

Deliberately NOT detected as violations:
  * ``jsonb_set`` / column-scoped updates that do not assign the whole blob;
  * DDL/trigger bodies in ``lib/database/_schema_*`` (they define the bump
    trigger itself, they are not transcript writers);
  * anything under ``tests/`` (fixtures legitimately seed rows directly).
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories whose blob writes are NOT transcript writers. Each entry must say
# why, so widening this set is a visible act rather than a quiet no-op.
_EXEMPT_PREFIXES = (
    'tests/',                      # fixtures seed rows directly by design
    'lib/database/_schema_pg/',    # defines the rev-bump trigger, not a writer
    'lib/database/_schema_sqlite/',
    'lib/database/messages_rows',  # the row-mirror target, not the blob owner
)

# Functions that are ALLOWED to overwrite unconditionally. Each needs a reason
# recorded at its definition site; the guard asserts the reason exists.
_SANCTIONED_UNCONDITIONAL = {
    'overwrite_conversation_messages_unconditional',
}

_UPDATE_CONV_RE = re.compile(r'update\s+conversations\b', re.IGNORECASE)
_SET_MESSAGES_RE = re.compile(r'\bset\b[^;]*?\bmessages\s*=', re.IGNORECASE | re.DOTALL)
_CAS_RE = re.compile(r'\bwhere\b[^;]*?\b(rev|updated_at)\s*=', re.IGNORECASE | re.DOTALL)


def _tracked_python_files() -> list[str]:
    """Repo-relative paths of git-tracked ``.py`` files.

    ``git ls-files`` (not ``os.walk``) because the project tree lives on a FUSE
    mount where a full walk times out, and because it automatically excludes
    untracked scratch files a sibling session may have left behind.
    """
    out = subprocess.run(
        ['git', 'ls-files', '*.py'],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    return [p for p in out.stdout.splitlines() if p.strip()]


def _is_exempt(rel_path: str) -> bool:
    return any(rel_path.startswith(p) for p in _EXEMPT_PREFIXES)


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    """Name of the innermost function containing ``lineno`` ('' at module level)."""
    best_name, best_start = '', -1
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, 'end_lineno', node.lineno)
        if node.lineno <= lineno <= end and node.lineno > best_start:
            best_name, best_start = node.name, node.lineno
    return best_name


def scan_writers() -> list[dict]:
    """Return one record per whole-blob ``conversations.messages`` write site.

    Each record: ``{path, line, func, cas, sql}`` where ``cas`` is True when the
    statement constrains ``rev`` or ``updated_at`` in its WHERE clause.
    """
    found: list[dict] = []
    for rel in _tracked_python_files():
        if _is_exempt(rel):
            continue
        abs_path = os.path.join(REPO_ROOT, rel)
        try:
            with open(abs_path, encoding='utf-8') as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if 'conversations' not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value
            if not _UPDATE_CONV_RE.search(sql):
                continue
            if not _SET_MESSAGES_RE.search(sql):
                continue
            found.append({
                'path': rel,
                'line': node.lineno,
                'func': _enclosing_function(tree, node.lineno),
                'cas': bool(_CAS_RE.search(sql)),
                'sql': ' '.join(sql.split()),
            })
    found.sort(key=lambda r: (r['path'], r['line']))
    return found


def unguarded_writers() -> list[dict]:
    """Whole-blob writers with neither a CAS predicate nor a sanctioned name."""
    return [w for w in scan_writers()
            if not w['cas'] and w['func'] not in _SANCTIONED_UNCONDITIONAL]


def _main() -> int:
    writers = scan_writers()
    print(f'scanned tracked .py files : {len(_tracked_python_files())}')
    print(f'whole-blob messages writes: {len(writers)}')
    print(f'  CAS-guarded             : {sum(1 for w in writers if w["cas"])}')
    print(f'  sanctioned unconditional: '
          f'{sum(1 for w in writers if not w["cas"] and w["func"] in _SANCTIONED_UNCONDITIONAL)}')
    print(f'  UNGUARDED               : {len(unguarded_writers())}')
    print()
    for w in writers:
        if w['cas']:
            mark = 'CAS '
        elif w['func'] in _SANCTIONED_UNCONDITIONAL:
            mark = 'OK* '
        else:
            mark = '>>> '
        print(f'{mark}{w["path"]}:{w["line"]}  {w["func"] or "<module>"}()')
        print(f'      {w["sql"][:150]}')
    return 0


if __name__ == '__main__':
    sys.exit(_main())
