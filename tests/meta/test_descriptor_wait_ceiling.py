"""Long-lived processes never wait on descriptors with ``select.select``.

``select()`` rejects descriptor numbers at or above FD_SETSIZE (1024), which
the API host, execution host and storage host reach (live 2026-11-07: a
command's pipes were fds 1817 and 4009). A swallowed ``ValueError`` turned the
wait into an instant return, a spinning loop and undrained command output
(docs/DEFENSIVE_PATTERNS.md, "Descriptor waits have no select() ceiling").
Wait with ``select.poll()``, ``selectors`` or ``lib.compat.safe_select_pipes``.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
LONG_LIVED_PACKAGES = ('lib', 'routes', 'serverctl_pkg', 'tofu_db', 'bootstrap_pkg')
# Each remaining call, and why it cannot meet a high descriptor.
ALLOWED = {
    'tofu_db/wire.py': 'runs only where the platform has no select.poll (Windows sockets)',
    'serverctl_pkg/native_storage_host.py': 'waits on the manager channel inherited at a fixed low descriptor',
}


def _tracked_python_files():
    listed = subprocess.run(['git', 'ls-files', '-z', '--', *LONG_LIVED_PACKAGES],
                            cwd=ROOT, check=True, capture_output=True).stdout
    return [name for name in listed.decode().split('\0') if name.endswith('.py')]


def _select_select_lines(relative):
    tree = ast.parse((ROOT / relative).read_text(encoding='utf-8'), filename=relative)
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom) and node.module == 'select'
                and any(alias.name == 'select' for alias in node.names)):
            yield node.lineno
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'select' and isinstance(node.func.value, ast.Name)
                and node.func.value.id == 'select'):
            yield node.lineno


def test_long_lived_processes_never_wait_with_select_select():
    files = _tracked_python_files()
    assert len(files) > 100, 'git ls-files listed too few sources to judge'
    offenders = [f'{relative}:{line}' for relative in files if relative not in ALLOWED
                 for line in _select_select_lines(relative)]
    assert not offenders, (
        'select.select() rejects descriptors >= 1024, which these processes reach; '
        'wait with select.poll(), selectors or lib.compat.safe_select_pipes: '
        + ', '.join(offenders))


def test_every_allowed_select_site_still_exists():
    for relative, reason in ALLOWED.items():
        assert list(_select_select_lines(relative)), (
            f'{relative} no longer calls select.select ({reason}); drop its exception')
