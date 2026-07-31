"""Contract: a row-mirror failure MUST NOT change the caller's behaviour.

**The incident (2026-07-27).** ``mirror_write_and_commit`` raised ``NameError``
(a dropped ``full`` parameter). At six call sites the hook sat INSIDE the same
``try`` block as the authoritative ``conversations.messages`` write, with an
``except`` that returns a failure value. So the sequence was:

  1. authoritative UPDATE commits  → the data IS durable
  2. mirror hook raises            → jumps to the shared ``except``
  3. caller returns ``None``/``False`` → "the write failed"

The most visible casualty was autopilot: ``_append_vu_message_to_conv``
returned ``None`` with the VU turn already committed, and
``maybe_run_autopilot`` reads ``None`` as "VU turn not persisted" and ENDS the
run. No follow-up task spawned, the carrier stayed orphaned at
``status=running``, and the budget counter never recorded the turn. Autopilot
just stopped, silently, with no error surfaced to the user.

Every docstring in ``lib/database/messages_rows.py`` promises the mirror is
best-effort and "can NEVER break the authoritative JSONB write path". That
promise was true of ``dual_write_conv`` (which swallows its own exceptions)
but NOT of the control flow at the call sites — a contract bug, not a name
bug, which is why the undefined-name ratchet could not have caught it.

**What this file pins**

* LAYER 1 — behavioural: with the hook monkeypatched to raise, the two
  highest-value callers still report success. This is the assertion that
  actually reproduces the incident.
* LAYER 2 — structural: an AST scan asserting no call site can route a mirror
  exception into an ``except`` that alters control flow. This catches the
  defect being reintroduced at a NEW call site, where a behavioural test
  wouldn't exist yet.
* LAYER 3 — the hook's own defence: ``mirror_write_and_commit`` must swallow
  everything, so even a buggy hook cannot reach a caller. Belt AND braces:
  the hook defends itself, and each call site defends against a
  signature-level ``TypeError`` that the callee cannot catch from inside.
"""

import ast
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


# ── LAYER 3: the hook itself must never raise ────────────────────────────

def test_hook_swallows_a_failing_backfill(monkeypatch):
    """``full=True`` path: an exploding backfill must not escape the hook."""
    import lib.database.messages_rows as mr

    monkeypatch.setattr(mr, 'rows_write_enabled', lambda: True)

    def _boom(*a, **kw):
        raise RuntimeError('backfill exploded')

    monkeypatch.setattr(mr, 'backfill_conv', _boom)

    class _DB:
        def commit(self):
            pass

    mr.mirror_write_and_commit(_DB(), 'c1', [], full=True)   # must not raise


def test_hook_swallows_a_failing_commit(monkeypatch):
    """A commit failure must not escape the hook."""
    import lib.database.messages_rows as mr

    monkeypatch.setattr(mr, 'rows_write_enabled', lambda: True)
    monkeypatch.setattr(mr, 'dual_write_conv', lambda *a, **kw: None)

    class _DB:
        def commit(self):
            raise RuntimeError('commit exploded')

    mr.mirror_write_and_commit(_DB(), 'c1', [])              # must not raise


def test_hook_swallows_a_failing_flag_probe(monkeypatch):
    """Even the flag probe raising must not escape (total defence)."""
    import lib.database.messages_rows as mr

    def _boom():
        raise RuntimeError('flag probe exploded')

    monkeypatch.setattr(mr, 'rows_write_enabled', _boom)

    class _DB:
        def commit(self):
            pass

    mr.mirror_write_and_commit(_DB(), 'c1', [])              # must not raise


# ── LAYER 1: callers must survive a raising mirror ───────────────────────

def test_autopilot_vu_append_survives_a_raising_mirror(monkeypatch):
    """THE incident: the VU turn is durable, so the append must report success.

    Returning ``None`` here is what silently ended the autopilot run.
    """
    import lib.database.messages_rows as mr
    import lib.tasks_pkg.autopilot_baton as baton

    def _boom(*a, **kw):
        raise RuntimeError('mirror exploded (simulating the NameError)')

    monkeypatch.setattr(mr, 'mirror_write_and_commit', _boom)

    committed = {}

    class _Cur:
        def __init__(self, row=None, rowcount=0):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class _DB:
        def execute(self, sql, params=None):
            # The append now reads (messages, rev) in ONE statement and writes
            # under a rev-CAS, so the fake row must carry the rev column and the
            # UPDATE must report rowcount — that IS the authoritative write.
            if sql.strip().upper().startswith('SELECT'):
                return _Cur(['[]', 0])
            if 'UPDATE' in sql.upper():
                committed['written'] = True
                return _Cur(None, rowcount=1)
            return _Cur(None)

        def commit(self):
            pass

    monkeypatch.setattr('lib.database.get_thread_db', lambda *a, **kw: _DB())

    out = baton._append_vu_message_to_conv('conv-x', 'vu-1', 'hello world')

    assert committed.get('written'), 'authoritative write should have happened'
    assert out is not None, (
        'a mirror failure returned None — the caller reads None as "VU turn '
        'not persisted" and ENDS the autopilot run, even though the turn is '
        'already committed. This is the 2026-07-27 incident.'
    )
    assert out['content'] == 'hello world'
    assert out['_isVirtualUser'] is True


def test_swarm_snapshot_survives_a_raising_mirror(monkeypatch):
    """A landed snapshot must not be reported as lost because the mirror failed."""
    import lib.database.messages_rows as mr

    def _boom(*a, **kw):
        raise RuntimeError('mirror exploded')

    monkeypatch.setattr(mr, 'mirror_write_and_commit', _boom)

    import json as _json

    import lib.swarm.snapshot as snap

    handle = _json.dumps({'agents': [{'id': 'a1'}]})
    messages = [{
        'role': 'assistant',
        'toolRounds': [{'toolName': 'spawn_agents', 'toolContent': handle}],
    }]

    class _Cur:
        def __init__(self, row, rowcount=1):
            self._row = row
            self.rowcount = rowcount

        def fetchone(self):
            return self._row

    class _DB:
        def execute(self, sql, params=None):
            if sql.startswith('SELECT messages'):
                return _Cur([_json.dumps(messages), 111, 7])
            if sql.startswith('UPDATE'):
                return _Cur(None, rowcount=1)
            return _Cur([7])

        def commit(self):
            pass

    monkeypatch.setattr('lib.database.get_thread_db', lambda *a, **kw: _DB())

    ok = snap.persist_snapshot_to_conversation(
        'conv-y', ['a1'],
        {'agents': [{'id': 'a1', 'status': 'done'}], 'settled': True,
         'version': 100001},
    )
    assert ok is True, (
        'the CAS UPDATE committed, so a mirror failure must not make this '
        'report the snapshot as not persisted'
    )


# ── LAYER 2: structural ratchet over every call site ─────────────────────

def _handler_alters_flow(handlers):
    for h in handlers:
        for n in ast.walk(h):
            if isinstance(n, (ast.Return, ast.Continue, ast.Break, ast.Raise)):
                return True
    return False


def _coupled_call_sites():
    """Call sites where a mirror exception would change the caller's flow."""
    out = subprocess.run(
        ['git', 'ls-files', 'lib/*.py', 'lib/**/*.py', 'routes/*.py', 'routes/**/*.py'],
        cwd=_ROOT, capture_output=True, text=True, check=True)
    files = [p for p in out.stdout.split('\n') if p.endswith('.py')]

    coupled = []
    for rel in files:
        try:
            with open(os.path.join(_ROOT, rel), encoding='utf-8') as f:
                src = f.read()
        except OSError:
            continue
        if 'mirror_write_and_commit' not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        tries = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Try):
                lines = set()
                for st in n.body:
                    for sub in ast.walk(st):
                        if hasattr(sub, 'lineno'):
                            lines.add(sub.lineno)
                tries.append((n, lines))

        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == 'mirror_write_and_commit'):
                continue
            enclosing = [(t, b) for t, b in tries if n.lineno in b]
            if not enclosing:
                continue                      # not in a try at all — safe
            innermost, _ = min(enclosing, key=lambda x: len(x[1]))
            # A dedicated wrapper (the mirror call + its import) is the fix.
            if len(innermost.body) <= 2:
                continue
            if _handler_alters_flow(innermost.handlers):
                coupled.append(f'{rel}:{n.lineno}')
    return coupled


def test_no_call_site_couples_mirror_failure_to_caller_flow():
    """No mirror call may sit in a try whose except alters control flow.

    Wrap the call in its own ``try/except`` that logs and continues. The
    authoritative write has already committed by the time the hook runs, so
    letting a mirror exception reach a shared handler makes the caller report
    failure for work that actually succeeded.
    """
    coupled = _coupled_call_sites()
    assert not coupled, (
        'mirror_write_and_commit call sites where a mirror failure would change '
        'the caller\'s return value / control flow:\n  ' + '\n  '.join(coupled)
        + '\n\nWrap each in its own try/except (log + continue).'
    )


def test_structural_ratchet_detects_a_coupled_site(tmp_path):
    """NEUTER: the pre-fix autopilot shape must be flagged by the AST scan."""
    broken = '''
def _append(conv_id, messages):
    try:
        db.execute('UPDATE conversations SET messages=?', (messages,))
        mirror_write_and_commit(db, conv_id, messages)
        return {'ok': True}
    except Exception as e:
        logger.error('append failed: %s', e)
        return None
'''
    tree = ast.parse(broken)
    tries = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Try):
            lines = set()
            for st in n.body:
                for sub in ast.walk(st):
                    if hasattr(sub, 'lineno'):
                        lines.add(sub.lineno)
            tries.append((n, lines))

    found = False
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'mirror_write_and_commit'):
            enclosing = [(t, b) for t, b in tries if n.lineno in b]
            innermost, _ = min(enclosing, key=lambda x: len(x[1]))
            if len(innermost.body) > 2 and _handler_alters_flow(innermost.handlers):
                found = True

    assert found, (
        'the structural ratchet failed to flag the exact pre-fix shape — it '
        'would not have prevented the 2026-07-27 incident'
    )
