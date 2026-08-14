"""Acceptance test for the production SQLite schema 47 → current upgrade."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_existing_v47_database_converges_to_scoped_locks_and_abort_fields(
        tmp_path):
    from lib.database import _core as core
    from lib.database._schema_sqlite._meta import _SCHEMA_VERSION

    snapshot = core.reset_sqlite_for_tests(str(tmp_path / 'schema-v47.db'))
    try:
        db = core.get_thread_db(core.DOMAIN_SYSTEM)
        db.execute('DROP TABLE scoped_sequences')
        db.execute('ALTER TABLE task_results DROP COLUMN abort_source')
        db.execute('ALTER TABLE task_results DROP COLUMN abort_requested_at')
        db.execute(
            "UPDATE schema_meta SET value='47' WHERE key='_schema_version'")
        db.commit()
        core.close_thread_db()

        core.init_db()

        db = core.get_thread_db(core.DOMAIN_SYSTEM)
        version = db.execute(
            "SELECT value FROM schema_meta WHERE key='_schema_version'"
        ).fetchone()[0]
        columns = {
            row['name'] for row in db.execute(
                'PRAGMA table_info(task_results)').fetchall()}
        table = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='scoped_sequences'").fetchone()
        running_index = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_task_running_completed'").fetchone()
        assert version == str(_SCHEMA_VERSION)
        assert {'abort_requested_at', 'abort_source'} <= columns
        assert table is not None
        assert running_index is not None
        normalized_index = ' '.join(running_index[0].lower().split())
        assert "where status='running' and completed_at is not null" in normalized_index
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT task_id, conv_id, completed_at "
            "FROM task_results WHERE status='running' "
            "AND completed_at IS NOT NULL AND completed_at < ? "
            "ORDER BY completed_at ASC LIMIT ?", (1, 200)).fetchall()
        assert 'idx_task_running_completed' in ' '.join(
            str(row['detail']) for row in plan)

        authority_plan = db.execute(
            'EXPLAIN QUERY PLAN SELECT c.id FROM conversations c '
            'INDEXED BY idx_conv_rows_authority '
            'LEFT JOIN conversation_messages cm ON cm.conv_id=c.id '
            'GROUP BY c.id,c.user_id,c.rev,c.messages_rows_rev,c.msg_count '
            'HAVING c.messages_rows_rev IS NULL '
            'OR c.messages_rows_rev<>c.rev OR COUNT(cm.seq)<>c.msg_count '
            'LIMIT 1').fetchall()
        assert 'idx_conv_rows_authority' in ' '.join(
            str(row['detail']) for row in authority_plan)
        projection_plan = db.execute(
            'EXPLAIN QUERY PLAN SELECT conv_id FROM conversation_messages '
            'WHERE meta_light IS NULL OR message_ts IS NULL '
            'OR billing_meta IS NULL LIMIT 1').fetchall()
        assert 'idx_conv_msgs_incomplete_projection' in ' '.join(
            str(row['detail']) for row in projection_plan)

        retention_indexes = {
            row['name'] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
                "('idx_task_terminal_retention',"
                " 'idx_task_events_stream_task_ts',"
                " 'idx_task_events_stream_ts')").fetchall()}
        assert retention_indexes == {
            'idx_task_terminal_retention',
            'idx_task_events_stream_task_ts',
        }
        retention_plan = db.execute(
            'EXPLAIN QUERY PLAN '
            'SELECT te.task_id,te.event_id FROM task_results tr '
            'INDEXED BY idx_task_terminal_retention '
            'CROSS JOIN task_events te '
            'INDEXED BY idx_task_events_stream_task_ts '
            "WHERE tr.status IN ('done','error','aborted','interrupted') "
            'AND tr.completed_at IS NOT NULL AND tr.completed_at < ? '
            'AND te.task_id=tr.task_id AND te.ts_ms < ? '
            "AND te.type NOT IN ('messages_snapshot','round_usage',"
            "'round_start','round_end') "
            'ORDER BY te.ts_ms ASC LIMIT ?', (1, 1, 25)).fetchall()
        retention_details = ' '.join(
            str(row['detail']) for row in retention_plan)
        assert 'idx_task_terminal_retention' in retention_details
        assert 'idx_task_events_stream_task_ts' in retention_details

        # Existing stream/queue rows seed their next-value floors during the
        # migration; an empty v47 fixture remains valid with an empty counter.
        assert db.execute(
            'SELECT COUNT(*) FROM scoped_sequences').fetchone()[0] == 0
    finally:
        core.restore_db_state(snapshot)
