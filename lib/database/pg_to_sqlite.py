"""Legacy in-memory export adapter kept for compatibility tests only.

.. warning::
   This module is **not a production cutover path**. Its historical adapter
   intentionally skips source/target schema differences and has no quiescence,
   cross-reopen, complete-source-table, or activation-attestation gate. The
   only production entry point is ``scripts/migrate_pg_to_sqlite.py`` followed
   by ``scripts/activate_sqlite_cutover.py``. ``main()`` below fails closed so
   two similar CLIs cannot issue contradictory success reports.

lib/database/pg_to_sqlite.py — PostgreSQL → SQLite 迁移导出器（P2）。

这是 storage.v1 生产切换前的历史离线转换器；双后端正式运行不调用它。

* **引擎解耦**：表结构来自 ``_core_schema`` 的共享 MetaData（双言同源），
  源端只读（PG 走 ``REPEATABLE READ READ ONLY`` 一致快照 + 服务器端游标流式
  读取，25GB 库不爆内存），目的端总是 SQLite。
* **row-equal 验证（owner 铁律）**：每张表两段校验——行数相等 + 逐行规范化
  哈希链相等。规范化吃掉 PG/SQLite 的表示差异（JSONB 键序/空白、BOOLEAN↔0/1、
  TIMESTAMPTZ↔TEXT、BYTEA memoryview↔bytes），哈希从**落盘后的目标文件**
  重读计算，不是从内存——真端到端。
* **目标库纪律**：目标文件必须先用项目自己的引导路径建 schema
  （``TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=<dst> python -c
  "from lib.database import init_db; init_db()"``），导出器拒绝写入任何
  非空表（防二次运行混进已用库）。``schema_meta`` 永远跳过——它是目标库
  自己引导出的版本标记，不是用户数据。
* **派生物跳过**：FTS5（``conversations_fts``）等 SQLite-only 派生表不在
  Core MetaData 里，天然不导出——切割后需重建（报告里会列出 post-step）。
* 源/目标缺表（PG-only 的 ``error_resolutions``、可选域 ``trading_config``）
  → 跳过并在报告中标注，不算失败。

CLI::

    # 1) 引导目标库 schema（项目自己的路径，保证与原生安装同形）
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=data/tofu_migrated.db \
        python -c "from lib.database import init_db; init_db()"
    # 2) 导出 + 校验（PG 连接取 TOFU_PG_* 环境变量）
    python -m lib.database.pg_to_sqlite --dst data/tofu_migrated.db \
        --report data/migration_report.json
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys

import sqlalchemy as sa

from lib.log import get_logger

logger = get_logger(__name__)

#: 永远跳过的表：目标库引导自产的版本标记，不是用户数据。
_ALWAYS_SKIP = frozenset({'schema_meta'})


def _is_pg(conn) -> bool:
    return 'psycopg' in type(conn).__module__


def _ph(conn) -> str:
    return '%s' if _is_pg(conn) else '?'


def _json_canon(v):
    """Canonical JSON text: parsed then re-dumped with sorted keys — eats
    JSONB's key-order/whitespace normalization so PG and SQLite text compare
    equal for the same logical value."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        v = bytes(v).decode('utf-8', 'replace')
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError as exc:
            logger.debug('[ExportCompat] non-JSON text kept verbatim: %s', exc)
            return v  # 非 JSON 文本原样保留
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'))
    return v


def _canon_value(v, col_type):
    """Normalize one column value for cross-engine comparison."""
    if v is None:
        return None
    if isinstance(col_type, sa.JSON):
        return _json_canon(v)
    if isinstance(col_type, sa.Boolean):
        if isinstance(v, str):
            return 1 if v.strip().lower() in ('t', 'true', '1', 'yes') else 0
        return int(bool(v))
    if isinstance(col_type, sa.DateTime):
        if isinstance(v, datetime.datetime):
            if v.tzinfo is not None:
                v = v.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            return v.isoformat(sep=' ')
        # SQLite 侧是 TEXT：尽量解析成同一形态；解析不了就原样比
        try:
            return datetime.datetime.fromisoformat(
                str(v).replace('Z', '+00:00')).replace(tzinfo=None).isoformat(sep=' ')
        except ValueError as exc:
            logger.debug('[ExportCompat] datetime text kept verbatim: %s', exc)
            return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return 'hex:' + bytes(v).hex()
    if isinstance(v, (dict, list)):
        return _json_canon(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def _to_sqlite_value(v, col_type):
    """Convert a source-driver value into something sqlite3 will store with
    the column affinity the project's schema declares."""
    if v is None:
        return None
    if isinstance(col_type, sa.JSON):
        c = _json_canon(v)
        return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    if isinstance(col_type, sa.Boolean):
        return _canon_value(v, col_type)
    if isinstance(col_type, sa.DateTime) and isinstance(v, datetime.datetime):
        if v.tzinfo is not None:
            v = v.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return v.isoformat(sep=' ')
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return v


def _quote(name: str) -> str:
    # 表/列名全部来自项目内 _core_schema 常量，无注入面；双引号保兼容。
    return '"%s"' % name.replace('"', '""')


def _order_cols(table: sa.Table) -> list:
    pk = [c.name for c in table.primary_key.columns]
    return pk or [c.name for c in table.columns]


def _table_exists(conn, name: str) -> bool:
    if _is_pg(conn):
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name=%s AND table_schema='public'", (name,))
        found = cur.fetchone() is not None
        cur.close()
        return found
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')",
        (name,))
    return cur.fetchone() is not None


def _row_count(conn, name: str) -> int:
    cur = conn.cursor() if _is_pg(conn) else conn
    q = 'SELECT COUNT(*) FROM %s' % _quote(name)
    cur = cur.execute(q) if _is_pg(conn) else cur.execute(q)
    return int(cur.fetchone()[0])


def _hash_update(h, vals) -> None:
    h.update(json.dumps(vals, ensure_ascii=False, default=str).encode('utf-8'))
    h.update(b'\n')


def _scan_hash(conn, table: sa.Table, batch_size: int):
    """Stream one table in PK order; return (row_count, sha256_hex).

    PG 用命名服务器端游标（客户端不缓冲全表）；SQLite fetchmany 本就流式。
    """
    cols = [c.name for c in table.columns]
    types = [c.type for c in table.columns]
    order = ','.join(_quote(c) for c in _order_cols(table))
    sql = 'SELECT %s FROM %s ORDER BY %s' % (
        ','.join(_quote(c) for c in cols), _quote(table.name), order)
    if _is_pg(conn):
        cur = conn.cursor(name='tofu_export_%s' % table.name)
        cur.itersize = batch_size
        cur.execute(sql)
    else:
        cur = conn.execute(sql)
    h = hashlib.sha256()
    n = 0
    while True:
        batch = cur.fetchmany(batch_size)
        if not batch:
            break
        for row in batch:
            _hash_update(h, [_canon_value(v, t) for v, t in zip(row, types)])
            n += 1
    if _is_pg(conn):
        cur.close()
    return n, h.hexdigest()


def export_database(src, dst, *, tables=None, batch_size: int = 1000,
                    progress=None) -> dict:
    """Export every registered Core table from `src` into `dst` + row-equal
    verification. Returns the per-table report (also logged).

    `src`: psycopg2 或 sqlite3 连接（只读使用）。
    `dst`: sqlite3 连接（目标文件，schema 须已由项目引导建好，各表须为空）。
    `tables`: 显式 Table 列表（测试用）；缺省 = Core 注册全集（FK 拓扑序）。
    """
    from lib.database import _core_schema as cs

    if tables is None:
        keep = set(cs._CORE_REGISTERED_TABLES)
        tables = [t for t in cs.metadata.sorted_tables if t.name in keep]

    dst.execute('PRAGMA journal_mode=WAL')
    dst.execute('PRAGMA synchronous=NORMAL')
    dst.execute('PRAGMA mmap_size=0')
    dst.execute('PRAGMA foreign_keys=OFF')

    if _is_pg(src):
        # 一致快照：全表导出在同一 REPEATABLE READ 只读事务里
        src.set_session(readonly=True)
        from psycopg2.extensions import ISOLATION_LEVEL_REPEATABLE_READ
        src.set_isolation_level(ISOLATION_LEVEL_REPEATABLE_READ)
    else:
        src.isolation_level = None
        src.execute('BEGIN')

    report = {'tables': {}, 'ok': True, 'skipped': [], 'post_steps': [
        '重建 FTS5 派生索引（conversations_fts 等不在 Core MetaData，未导出）',
        'PG-only 表 error_resolutions 在 SQLite 引导中不存在——如源端有数据，'
        '其行已随源保留在 PG 归档，未迁移',
    ]}
    try:
        for table in tables:
            name = table.name
            if name in _ALWAYS_SKIP:
                report['skipped'].append({'table': name, 'reason': 'always-skip'})
                continue
            if not _table_exists(src, name):
                report['skipped'].append({'table': name, 'reason': 'missing-in-src'})
                continue
            if not _table_exists(dst, name):
                report['skipped'].append({'table': name, 'reason': 'missing-in-dst'})
                continue
            existing = _row_count(dst, name)
            if existing:
                report['tables'][name] = {
                    'ok': False,
                    'error': f'target table not empty ({existing} rows) — '
                             'refusing to mix into a used database',
                }
                report['ok'] = False
                logger.error('[Export] %s: target not empty (%d rows) — abort '
                             'for this table', name, existing)
                continue

            cols = [c.name for c in table.columns]
            types = [c.type for c in table.columns]
            order = ','.join(_quote(c) for c in _order_cols(table))
            select_sql = 'SELECT %s FROM %s ORDER BY %s' % (
                ','.join(_quote(c) for c in cols), _quote(name), order)
            insert_sql = 'INSERT INTO %s (%s) VALUES (%s)' % (
                _quote(name), ','.join(_quote(c) for c in cols),
                ','.join(['?'] * len(cols)))

            if _is_pg(src):
                cur = src.cursor(name='tofu_export_%s' % name)
                cur.itersize = batch_size
                cur.execute(select_sql)
            else:
                cur = src.execute(select_sql)
            src_h = hashlib.sha256()
            n = 0
            while True:
                batch = cur.fetchmany(batch_size)
                if not batch:
                    break
                for row in batch:
                    _hash_update(src_h,
                                 [_canon_value(v, t) for v, t in zip(row, types)])
                dst.executemany(
                    insert_sql,
                    [tuple(_to_sqlite_value(v, t) for v, t in zip(row, types))
                     for row in batch])
                n += len(batch)
            if _is_pg(src):
                cur.close()
            dst.commit()

            # row-equal 验证：行数 + 从落盘目标文件重读的哈希
            dst_n, dst_h = _scan_hash(dst, table, batch_size)
            ok = (dst_n == n and dst_h == src_h.hexdigest())
            report['tables'][name] = {
                'ok': ok, 'rows': n, 'dst_rows': dst_n,
                'sha256': src_h.hexdigest(),
            }
            if not ok:
                report['ok'] = False
                logger.error('[Export] %s: MISMATCH rows %d→%d hash %s vs %s',
                             name, n, dst_n, src_h.hexdigest()[:12],
                             dst_h[:12])
            else:
                logger.info('[Export] %s: %d rows, sha256 %s ✓',
                            name, n, src_h.hexdigest()[:12])
            if progress:
                try:
                    progress(name, n, ok)
                except Exception as e:
                    logger.debug('[Export] progress callback failed: %s', e)
    finally:
        if _is_pg(src):
            try:
                src.rollback()
            except Exception as e:
                logger.debug('[Export] src snapshot close failed: %s', e)
        else:
            try:
                src.execute('ROLLBACK')
            except Exception as e:
                logger.debug('[Export] src snapshot rollback failed: %s', e)
        try:
            dst.execute('PRAGMA foreign_keys=ON')
        except Exception as e:
            logger.debug('[Export] re-enable foreign_keys failed: %s', e)

    # FK 体检（加载期 foreign_keys=OFF）
    try:
        violations = dst.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            report['ok'] = False
            report['fk_violations'] = len(violations)
            logger.error('[Export] foreign_key_check: %d violation(s)',
                         len(violations))
    except Exception as e:
        logger.warning('[Export] foreign_key_check failed (non-fatal): %s', e)

    n_ok = sum(1 for t in report['tables'].values() if t.get('ok'))
    report['summary'] = {
        'tables_ok': n_ok,
        'tables_failed': sum(1 for t in report['tables'].values()
                             if not t.get('ok')),
        'tables_skipped': len(report['skipped']),
        'total_rows': sum(t.get('rows', 0)
                          for t in report['tables'].values()),
    }
    logger.info('[Export] done: %s', report['summary'])
    return report


def main(argv=None) -> int:
    del argv
    logger.error(
        '[Export] Legacy lib.database.pg_to_sqlite CLI is disabled: it can '
        'skip source tables and cannot attest a quiesced cutover. Use '
        'scripts/migrate_pg_to_sqlite.py and '
        'scripts/activate_sqlite_cutover.py.')
    return 2


if __name__ == '__main__':
    sys.exit(main())
