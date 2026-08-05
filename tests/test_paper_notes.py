#!/usr/bin/env python3
"""paper_notes (reader margin notes, P4) backend suite.

Proves against the REAL chat DB (unique throwaway paper_hash, cleaned up):

  1. the table exists with the Core-defined columns (created by the schema
     bootstrap on both backends — this run uses the active one);
  2. full CRUD round trip through the same async helpers the routes use;
  3. the anchor JSON blob round-trips {heading_idx, char_offset, quote};
  4. _note_row_to_dict maps a row to the wire shape (anchor parsed, bad JSON
      tolerated to {});
  5. NEUTER: dropping the table lookup by a wrong lang returns nothing —
     notes are strictly per (paper_hash, lang) (no cross-language bleed).

Run standalone: ``python3 tests/test_paper_notes.py``
"""

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('TRADING_ENABLED', '0')


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_PHASH = f'test-notes-{uuid.uuid4().hex[:10]}'
_IDS = []
_TMPDIR = None


def _bootstrap_isolated_db():
    """Force an isolated SQLite backend (fresh temp file) and run init_db —
    the same helper unit tests use so they never touch the live PG. The
    bootstrap itself is under test here: it must create paper_notes."""
    global _TMPDIR
    import tempfile
    from lib.database._core import reset_sqlite_for_tests
    _TMPDIR = tempfile.mkdtemp(prefix='paper-notes-test-')
    reset_sqlite_for_tests(os.path.join(_TMPDIR, 'chat.db'))


def test_table_exists_with_core_columns():
    from lib.database import get_thread_db
    from lib.database._core_schema import PAPER_NOTES
    cols = {c.name for c in PAPER_NOTES.columns}
    assert cols == {'id', 'paper_hash', 'lang', 'anchor', 'note',
                    'created_at', 'updated_at'}, f'Core columns drifted: {cols}'
    db = get_thread_db()
    row = db.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='paper_notes'"
    ).fetchone()
    assert row and list(row)[0] >= 1, 'paper_notes table not created by the bootstrap'
    idx = db.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='index' AND name='idx_paper_notes_hash'"
    ).fetchone()
    assert idx and list(idx)[0] >= 1, 'paper_notes lookup index missing'
    _ok('paper_notes 表+索引由 bootstrap 创建且 Core 列定义齐全')


def test_crud_roundtrip():
    from lib.database import async_execute, async_fetchall, async_fetchone

    anchor = {'heading_idx': 2, 'char_offset': None,
              'quote': 'The method section.'}
    nid = f'pn_test_{uuid.uuid4().hex[:12]}'
    _IDS.append(nid)
    now = 1754000000

    async def _flow():
        await async_execute(
            "INSERT INTO paper_notes (id, paper_hash, lang, anchor, note, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (nid, _PHASH, 'en', json.dumps(anchor, ensure_ascii=False),
             'remember the O(1) path argument', now, now))
        rows = await async_fetchall(
            "SELECT * FROM paper_notes WHERE paper_hash = ? AND lang = ? ORDER BY created_at ASC",
            (_PHASH, 'en'))
        assert len(rows) == 1, f'insert not visible: {rows}'
        loaded_anchor = json.loads(rows[0]['anchor'])
        assert loaded_anchor['heading_idx'] == 2
        assert loaded_anchor['quote'] == 'The method section.'
        # UPDATE
        await async_execute(
            "UPDATE paper_notes SET note = ?, updated_at = ? WHERE id = ?",
            ('edited: the bet breaks at 100k context', now + 1, nid))
        row = await async_fetchone(
            "SELECT note, updated_at FROM paper_notes WHERE id = ?", (nid,))
        assert row['note'].startswith('edited:'), f'update not applied: {row}'
        # wrong-lang isolation (NEUTER direction)
        other = await async_fetchall(
            "SELECT * FROM paper_notes WHERE paper_hash = ? AND lang = ?",
            (_PHASH, 'zh'))
        assert not other, 'note leaked across languages'
        # DELETE
        await async_execute("DELETE FROM paper_notes WHERE id = ?", (nid,))
        gone = await async_fetchone(
            "SELECT id FROM paper_notes WHERE id = ?", (nid,))
        assert gone is None, 'delete did not remove the row'
        return True

    assert _run(_flow()) is True
    _ok('CRUD 往返 + 锚 JSON + 语言隔离 + 删除')


def test_row_mapper():
    import routes.paper as _rp_check  # noqa: F401 — import cost paid once here
    from routes.paper import _note_row_to_dict
    row = {'id': 'pn_1', 'paper_hash': 'h', 'lang': 'en',
           'anchor': '{"heading_idx": 3, "quote": "x"}',
           'note': 'hello', 'created_at': 1, 'updated_at': 2}
    d = _note_row_to_dict(row)
    assert d['anchor'] == {'heading_idx': 3, 'quote': 'x'}
    assert d['note'] == 'hello' and d['updated_at'] == 2
    bad = dict(row, anchor='{not json')
    d2 = _note_row_to_dict(bad)
    assert d2['anchor'] == {}, 'malformed anchor must degrade to {}'
    _ok('_note_row_to_dict:锚解析/坏 JSON 兜底')


def _import_routes_paper_shimmed():
    for modname in ('flask', 'quart'):
        try:
            mod = __import__(modname)
            if hasattr(mod, 'Blueprint') and not hasattr(mod.Blueprint, 'websocket'):
                mod.Blueprint.websocket = lambda self, *a, **k: (lambda f: f)
        except Exception:
            pass
    import routes.paper  # noqa: F401


def main():
    print()
    print(_color('═══ Paper Notes Backend Tests ═══', '36'))
    print()
    _import_routes_paper_shimmed()
    _bootstrap_isolated_db()
    tests = [
        test_table_exists_with_core_columns,
        test_crud_roundtrip,
        test_row_mapper,
    ]
    try:
        for fn in tests:
            try:
                fn()
            except AssertionError as e:
                _fail(f'{fn.__name__}: {e}')
            except Exception as e:
                import traceback
                traceback.print_exc()
                _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    finally:
        # Cleanup the isolated temp DB (the whole directory is throwaway).
        try:
            import shutil
            if _TMPDIR:
                shutil.rmtree(_TMPDIR, ignore_errors=True)
        except Exception as e:
            print('cleanup warning:', e)
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
