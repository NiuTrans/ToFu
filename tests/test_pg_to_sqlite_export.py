"""tests/test_pg_to_sqlite_export.py — P2 导出器（PG→SQLite row-equal）。

历史离线迁移闸门（不是 storage.v1 运行路径）：逐表
row-count + 校验和相等才放行。本套件用 sqlite→sqlite 的双连接对演练
全部导出/校验逻辑（PG 侧差异仅在游标与占位符分支，生产首跑前另有
真实 PG 演练步骤）：

  1. 全表导出后行数 + 规范化哈希相等（report.ok）。
  2. JSON 规范化吃键序差异（JSONB 重排键 vs TEXT 原样——同一逻辑值同哈希）。
  3. BOOLEAN/时间戳/blob 的跨引擎表示归一。
  4. 目标表非空 → 拒绝混入（防二次运行污染已用库）。
  5. 批边界：batch_size=2 导 5 行，零丢失。
  6. 篡改检测：改目标库一行 → 校验翻红并指名表。

NEUTER：让 _canon_value 对 JSON 列直接返回原值（不规范化）→ 针 2 翻红，
证明规范化是承力的。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE users ('
                 'id TEXT PRIMARY KEY, name TEXT, admin BOOLEAN, '
                 'created TEXT, prefs TEXT, avatar BLOB)')
    conn.execute('CREATE TABLE task_events ('
                 'task_id TEXT, event_id INTEGER, ts_ms INTEGER, '
                 'type TEXT, payload TEXT, PRIMARY KEY (task_id, event_id))')
    return conn


def _seed_src(src):
    src.execute("INSERT INTO users VALUES ('u1','Alice',1,"
                "'2026-08-07 12:00:00',"
                "'{\"theme\": \"dark\", \"lang\": \"zh\"}',?)",
                (sqlite3.Binary(b'\x89PNG'),))
    src.execute("INSERT INTO users VALUES ('u2','Bob',0,"
                "'2026-08-07 13:00:00','{}',NULL)")
    for i in range(5):
        src.execute(
            'INSERT INTO task_events VALUES (?,?,?,?,?)',
            ('t1', i, 1700000000000 + i, 'delta',
             json.dumps({'type': 'delta', 'i': i}, ensure_ascii=False)))
    src.commit()


def _tables():
    import sqlalchemy as sa
    meta = sa.MetaData()
    users = sa.Table(
        'users', meta,
        sa.Column('id', sa.Text, primary_key=True),
        sa.Column('name', sa.Text),
        sa.Column('admin', sa.Boolean),
        sa.Column('created', sa.DateTime),
        sa.Column('prefs', sa.JSON),
        sa.Column('avatar', sa.LargeBinary),
    )
    task_events = sa.Table(
        'task_events', meta,
        sa.Column('task_id', sa.Text, primary_key=True),
        sa.Column('event_id', sa.Integer, primary_key=True),
        sa.Column('ts_ms', sa.BigInteger),
        sa.Column('type', sa.Text),
        sa.Column('payload', sa.JSON),
    )
    return [users, task_events]


@pytest.fixture()
def pair(tmp_path):
    src = _make_db(str(tmp_path / 'src.db'))
    _seed_src(src)
    dst = _make_db(str(tmp_path / 'dst.db'))
    yield src, dst
    src.close()
    dst.close()


def test_export_row_equal(pair):
    from lib.database.pg_to_sqlite import export_database
    src, dst = pair
    rep = export_database(src, dst, tables=_tables(), batch_size=100)
    assert rep['ok'], rep
    assert rep['tables']['users']['rows'] == 2
    assert rep['tables']['task_events']['rows'] == 5
    # 落盘内容逐字段相等
    rows = dst.execute('SELECT * FROM users ORDER BY id').fetchall()
    assert rows[0][1] == 'Alice' and rows[0][2] == 1
    assert json.loads(rows[0][4]) == {'theme': 'dark', 'lang': 'zh'}
    assert bytes(rows[0][5]) == b'\x89PNG'


def test_json_canonicalization_eats_key_order():
    from lib.database.pg_to_sqlite import _json_canon
    import sqlalchemy as sa
    a = _json_canon('{"b": 1, "a": [2, 3]}')
    b = _json_canon({'a': [2, 3], 'b': 1})
    assert a == b == '{"a":[2,3],"b":1}'
    assert _json_canon('plain text') == 'plain text'
    assert _json_canon(None) is None


def test_cross_engine_repr_normalization():
    import sqlalchemy as sa
    import datetime
    from lib.database.pg_to_sqlite import _canon_value
    assert _canon_value(True, sa.Boolean()) == 1
    assert _canon_value('t', sa.Boolean()) == 1
    assert _canon_value(0, sa.Boolean()) == 0
    dt = datetime.datetime(2026, 8, 7, 12, 0,
                           tzinfo=datetime.timezone.utc)
    assert _canon_value(dt, sa.DateTime()) == '2026-08-07 12:00:00'
    assert _canon_value('2026-08-07 12:00:00', sa.DateTime()) == \
        '2026-08-07 12:00:00'
    assert _canon_value(memoryview(b'\x01\x02'), sa.LargeBinary()) == 'hex:0102'


def test_refuses_non_empty_target(pair):
    from lib.database.pg_to_sqlite import export_database
    src, dst = pair
    dst.execute("INSERT INTO users (id) VALUES ('squatter')")
    dst.commit()
    rep = export_database(src, dst, tables=_tables(), batch_size=100)
    assert not rep['ok']
    assert 'not empty' in rep['tables']['users']['error']
    # 未被污染：占位行还在，导出行没进来
    n = dst.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    assert n == 1
    # task_events 是空的 → 那张表照常导出（按表隔离）
    assert rep['tables']['task_events']['ok']


def test_batch_boundary(pair):
    from lib.database.pg_to_sqlite import export_database
    src, dst = pair
    rep = export_database(src, dst, tables=_tables(), batch_size=2)
    assert rep['ok'], rep
    assert dst.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == 5


def test_tamper_detection(pair):
    from lib.database.pg_to_sqlite import export_database, _scan_hash
    src, dst = pair
    rep = export_database(src, dst, tables=_tables(), batch_size=100)
    assert rep['ok']
    dst.execute("UPDATE task_events SET payload='{\"tampered\": true}' "
                "WHERE task_id='t1' AND event_id=3")
    dst.commit()
    tables = _tables()
    te = [t for t in tables if t.name == 'task_events'][0]
    _, bad_h = _scan_hash(dst, te, 100)
    assert bad_h != rep['tables']['task_events']['sha256'], (
        'tampering did not move the hash — the checksum is not load-bearing')


def test_legacy_cli_fails_closed_instead_of_skipping_production_tables():
    from lib.database.pg_to_sqlite import main

    assert main([]) == 2


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
