#!/usr/bin/env python3
"""tests/test_messages_rows_mirror_hook_callable.py — the hook must actually RUN.

``tests/test_messages_rows_hook_coverage.py`` proves every blob writer *mentions*
``mirror_write_and_commit``.  It is a source scanner, so it stayed green while
the hook itself raised ``NameError: name 'full' is not defined`` on EVERY call
(commit c1d40b33 shipped a body branching on ``full`` that was never added to
the signature).  With the write flag on, all 26 fan-out sites failed —
observed in production as autopilot silently declining to take over
(``_append_vu_message_to_conv`` catches the exception and returns ``None``,
which ``maybe_run_autopilot`` reads as "VU declined").

These tests execute the hook against a fake DB with the write flag FORCED ON,
which is the only way to catch a break inside the flag-gated branch: with the
flag off the function short-circuits before touching anything.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))


class _FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeDB:
    """Minimal stand-in: records statements, answers the COUNT probe with 0."""

    def __init__(self):
        self.statements = []
        self.commits = 0

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if 'COUNT(*)' in sql:
            return _FakeCursor({'n': 0})
        return _FakeCursor(None)

    def commit(self):
        self.commits += 1


@pytest.fixture
def mirror(monkeypatch):
    from lib.database import messages_rows as mr
    # Force the write flag ON — the whole body is behind it, so a flag-off run
    # would vacuously pass no matter how broken the branch is.
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '1')
    assert mr.rows_write_enabled(), 'precondition: write flag must be on'
    return mr


_MSGS = [
    {'role': 'user', 'content': 'hi', '_msgId': 'm1'},
    {'role': 'assistant', 'content': 'hello', '_msgId': 'm2'},
]


def test_incremental_call_does_not_raise(mirror):
    """The default (tail-append) shape — 20 of the 26 fan-out sites."""
    db = _FakeDB()
    mirror.mirror_write_and_commit(db, 'conv-a', _MSGS, now_ms=123)
    assert db.commits == 1, 'mirror must commit its rows (pt_7e4afe73 durability)'


def test_full_rebuild_call_does_not_raise(mirror):
    """``full=True`` — the REWRITE-class writers (reconcile / killed-recovery /
    feishu trim / persistence_store).  The keyword is documented in the
    docstring and passed by 7 call sites, so it MUST be accepted."""
    db = _FakeDB()
    mirror.mirror_write_and_commit(db, 'conv-a', _MSGS, now_ms=123, full=True)
    assert db.commits == 1
    assert any('DELETE FROM conversation_messages' in s for s, _ in db.statements), \
        'full=True must take the rebuild path (DELETE + re-insert)'


def test_changed_seqs_call_does_not_raise(mirror):
    """The seq-hint shape used by translate-commit / patch-by-id / swarm."""
    db = _FakeDB()
    mirror.mirror_write_and_commit(db, 'conv-a', _MSGS, now_ms=123, changed_seqs=[1])
    assert db.commits == 1


def test_flag_off_is_a_pure_noop(mirror, monkeypatch):
    """The documented contract: flag off == byte-identical to not calling."""
    monkeypatch.setenv('TOFU_MESSAGES_ROWS', '0')
    db = _FakeDB()
    mirror.mirror_write_and_commit(db, 'conv-a', _MSGS, full=True)
    assert db.statements == [] and db.commits == 0


def test_hook_is_exported(mirror):
    """A stale duplicate ``__all__`` dropped the hook from the public surface;
    the last assignment wins, so a star-import lost it."""
    assert 'mirror_write_and_commit' in mirror.__all__


def test_every_caller_keyword_is_in_the_signature():
    """Guard the class of bug directly: scan the real call sites and assert the
    signature accepts every keyword they pass.  A source scanner that only
    greps for the NAME cannot see an arity mismatch."""
    import inspect
    import re
    import subprocess

    from lib.database.messages_rows import mirror_write_and_commit

    root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
    params = set(inspect.signature(mirror_write_and_commit).parameters)

    out = subprocess.run(['git', 'ls-files', 'lib/*.py', 'routes/*.py'],
                         cwd=root, capture_output=True, text=True, check=True)
    # Match CALL sites only — `def mirror_write_and_commit(...)` itself carries
    # annotated defaults (`now_ms: int = 0`) that would otherwise read as a
    # keyword named `int`.
    call_re = re.compile(r'(?<!def )mirror_write_and_commit\(([^)]*)\)', re.DOTALL)
    kw_re = re.compile(r'(\w+)\s*=(?!=)')

    bad = []
    for path in out.stdout.split():
        if (not path.endswith('.py')
                or not os.path.isfile(os.path.join(root, path))):
            continue
        with open(os.path.join(root, path), encoding='utf-8') as f:
            src = f.read()
        if 'mirror_write_and_commit(' not in src:
            continue
        for args in call_re.findall(src):
            for kw in kw_re.findall(args):
                if kw not in params:
                    bad.append(f'{path}: passes `{kw}=` which is not in the signature')
    assert not bad, 'call sites use unknown keywords:\n  ' + '\n  '.join(sorted(set(bad)))


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
