#!/usr/bin/env python3
"""Tests for lib/ids.short_id — the single home for hand-rolled short ids.

~40 sites across lib/ + routes/ hand-rolled ``uuid.uuid4().hex[:N]`` (often with
a prefix). This suite pins short_id's byte-shape contract (so migrations are
provably behavior-preserving), the compat re-export, and a static guard that
the migrated files no longer carry the raw idiom.

Run standalone (``python tests/test_ids_short_id.py``) or via pytest.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

_HEX = set('0123456789abcdef')
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_default_shape():
    from lib.ids import short_id
    v = short_id()
    assert len(v) == 24 and all(c in _HEX for c in v), v
    _ok('short_id(): 24 lowercase-hex chars, no prefix')


def test_prefix_and_length():
    from lib.ids import short_id
    v = short_id('led_')
    assert v.startswith('led_') and len(v) == len('led_') + 24
    assert all(c in _HEX for c in v[len('led_'):])
    w = short_id('pt_', 16)
    assert w.startswith('pt_') and len(w) == len('pt_') + 16
    b = short_id(n=8)
    assert len(b) == 8 and all(c in _HEX for c in b)
    _ok('short_id(prefix, n): prefix prepended verbatim, n hex tail')


def test_uniqueness():
    from lib.ids import short_id
    ids = {short_id('x_') for _ in range(2000)}
    assert len(ids) == 2000, 'collisions in 2000 draws'
    _ok('short_id: unique across 2000 draws')


def test_byte_equiv_to_old_idiom():
    """short_id(p, n) must be indistinguishable from the old
    f'{p}{uuid.uuid4().hex[:n]}' — same prefix + hex alphabet + length."""
    import uuid
    from lib.ids import short_id
    for prefix, n in [('', 24), ('chatcmpl-', 24), ('msg_', 16), ('pt_', 16),
                      ('hg_', 12), ('todo-', 8), ('', 12)]:
        old = f'{prefix}{uuid.uuid4().hex[:n]}'
        new = short_id(prefix, n)
        assert new.startswith(prefix)
        assert len(new) == len(old)
        assert new[len(prefix):] and all(c in _HEX for c in new[len(prefix):])
    _ok('short_id: byte-shape-equivalent to the replaced idiom')


def test_compat_reexport_is_same_object():
    from lib.ids import short_id as canonical
    from lib.compat._common import short_id as reexported
    assert reexported is canonical, 'compat._common.short_id must re-export lib.ids.short_id'
    _ok('lib.compat._common.short_id re-exports the canonical lib.ids.short_id')


def test_migrated_sites_have_no_raw_idiom():
    """Static guard: the migrated files no longer hand-roll uuid4().hex[:.

    Only files this batch migrated are checked. Deliberately EXCLUDED:
      * lib/ids.py + lib/compat/_common.py docstrings mention the idiom
      * lib/log.py (dependency-free foundational module — cannot import lib.ids)
      * the 3 sibling-dirty files skipped this batch (scheduler/timer/_poll.py,
        tasks_pkg/autopilot.py, tasks_pkg/tool_dispatch/_parse.py)
    """
    migrated = [
        'lib/billing/ledger.py', 'lib/billing/wallet.py', 'lib/billing/users.py',
        'lib/billing/payments/_common.py',
        'lib/conversations/project_watch.py', 'lib/conversations/project_charter.py',
        'lib/conversations/project_board.py',
        'lib/optimizer/storage.py', 'lib/memory/user_profile/_pending.py',
        'lib/pdf_parser/vlm/_tasks.py', 'lib/daily_report/conversations.py',
        'lib/agent_core/task_runtime.py',
        'lib/tasks_pkg/handlers/code_exec.py',
        'lib/tasks_pkg/compaction/_layer2/_compact.py',
        'lib/tasks_pkg/sync_run.py',
        'lib/tasks_pkg/handlers/misc/_human.py',
        'lib/tasks_pkg/handlers/misc/_brain.py',
        'routes/compat_anthropic.py', 'routes/compat_openai.py',
        'routes/api_v1/agent_run.py', 'routes/api_v1/billing.py',
        'routes/api_v1/chat.py', 'routes/api_v1/chat_direct.py',
        'routes/api_v1/daily_report.py',
    ]
    pat = re.compile(r'uuid\.uuid4\(\)\.hex\[:')
    offenders = []
    for rel in migrated:
        p = os.path.join(_ROOT, rel)
        try:
            src = open(p, encoding='utf-8').read()
        except OSError:
            continue
        if pat.search(src):
            offenders.append(rel)
    assert not offenders, f'raw uuid4().hex[: idiom still present in: {offenders}'
    _ok(f'migration: {len(migrated)} files carry no raw uuid4().hex[: idiom')


def main():
    print()
    print(_color('═══ lib/ids.short_id Tests ═══', '36'))
    print()
    tests = [
        test_default_shape,
        test_prefix_and_length,
        test_uniqueness,
        test_byte_equiv_to_old_idiom,
        test_compat_reexport_is_same_object,
        test_migrated_sites_have_no_raw_idiom,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
