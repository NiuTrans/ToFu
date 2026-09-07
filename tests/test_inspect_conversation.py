"""Guard tests for debug/inspect_conversation.py.

The script is the documented first step for conversation-ID debugging
(AGENTS.md). These tests pin its contract against a disposable sqlite file:
location probes, turn-native projection, rendering, and the not-found exit
code.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_inspector_is_declared_in_every_source_delivery_boundary():
    root = Path(__file__).resolve().parents[1]
    assert '!/debug/inspect_conversation.py' in (
        root / '.gitignore').read_text(encoding='utf-8')
    assert '!debug/inspect_conversation.py' in (
        root / '.dockerignore').read_text(encoding='utf-8')
    assert "'debug/inspect_conversation.py'" in (
        root / 'export.py').read_text(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'debug' / 'inspect_conversation.py'


def _create_sidecar_schema(db_path: Path) -> None:
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE storage_conversations (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT,
            messages_json TEXT,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            settings_json TEXT,
            msg_count INTEGER,
            search_text TEXT,
            rev INTEGER
        );
        CREATE TABLE storage_conversation_turns (
            turn_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            lane_id TEXT,
            parent_turn_id TEXT,
            ordinal INTEGER,
            actor TEXT,
            kind TEXT,
            run_id TEXT,
            status TEXT,
            current_attempt_id TEXT,
            projection_json TEXT,
            projection_revision INTEGER,
            settlement_json TEXT,
            created_at INTEGER,
            updated_at INTEGER
        );
        CREATE TABLE storage_compaction_archives (
            archive_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            messages_json BLOB NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            receipt_json BLOB NOT NULL DEFAULT '{}',
            trigger TEXT NOT NULL DEFAULT 'force',
            task_id TEXT NOT NULL DEFAULT '',
            round_num INTEGER NOT NULL DEFAULT 0,
            model TEXT NOT NULL DEFAULT '',
            tokens_before INTEGER NOT NULL DEFAULT 0,
            tokens_after INTEGER NOT NULL DEFAULT 0,
            msgs_before INTEGER NOT NULL DEFAULT 0,
            msgs_after INTEGER NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            payload_size INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()


def _insert_turn_native_conversation(db_path: Path, conv_id: str) -> None:
    connection = sqlite3.connect(db_path)
    connection.execute(
        'INSERT INTO storage_conversations VALUES (?,?,?,?,?,?,?,?,?,?)',
        (conv_id, 1, 'turn title', '[]',
         1787213775740, 1787213970001, '{}', 0, '', 244))
    connection.execute(
        'INSERT INTO storage_conversation_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('turn-1', conv_id, 1, 'main', None, 0, 'human', 'input', '', 'completed',
         None, json.dumps({'role': 'user', 'content': 'hello turns',
                           'timestamp': 1787213775740}),
         1, '{}', 1787213775740, 1787213775740))
    connection.commit()
    connection.close()


def _run(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, '--db', str(db_path),
         '--no-logs'],
        capture_output=True, text=True, timeout=60)


def _run_auto(data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment['TOFU_DATA_DIR'] = str(data_dir)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, '--no-logs'],
        capture_output=True, text=True, timeout=60, env=environment)


def test_turn_native_conversation_projects_transcript(tmp_path):
    """messages_json=[] + msg_count=0 must still render the turns content."""
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-turn')
    result = _run(db_path, 'conv-turn')
    assert result.returncode == 0, result.stderr
    assert 'HIT  storage_conversation_turns' in result.stdout
    assert 'turn title' in result.stdout
    assert 'hello turns' in result.stdout
    assert 'turn=completed' in result.stdout
    assert 'cause=ingested' in result.stdout


def test_inspector_auto_discovers_authority_from_live_lease(tmp_path):
    from lib.storage_sidecar.preflight import ProjectLease

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    db_path = data_dir / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-auto')
    lease = ProjectLease(data_dir)
    lease.acquire()
    try:
        lease.publish_storage_locator({
            'format': 'tofu.storage-locator/v1',
            'backend': 'sqlite',
            'authority_path': str(db_path.resolve()),
            'configured_path': str(db_path.resolve()),
            'fastpath_active': False,
        })
        result = _run_auto(data_dir, 'conv-auto')
    finally:
        lease.release()

    assert result.returncode == 0, result.stderr
    assert 'discovery:  live_lease_locator' in result.stdout
    assert 'hello turns' in result.stdout


def test_inspector_renders_compaction_summary_and_receipt(tmp_path):
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-compact')
    connection = sqlite3.connect(db_path)
    summary = (
        '### Objective\nDownload two skills and improve both MCP tools.\n\n'
        '### Current Working State\nLogin is only an immediate obstacle.')
    receipt = {'status': 'completed', 'strategy': 'selective_summary',
               'objectiveAnchored': True}
    connection.execute(
        'INSERT INTO storage_compaction_archives VALUES '
        '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('archive-1', 'conv-compact', 1, '[]', summary,
         json.dumps(receipt), 'working_set', 'task-1', 32, 'model',
         147641, 25982, 88, 2, 'working_set', 2, 1787213975000))
    connection.commit()
    connection.close()

    result = _run(db_path, 'conv-compact')

    assert result.returncode == 0, result.stderr
    assert '== Compaction Archives == (1 archive(s), latest 20)' in result.stdout
    assert 'Download two skills and improve both MCP tools' in result.stdout
    assert 'objectiveAnchored' in result.stdout


def test_authority_discovery_prefers_manifest_proven_fastpath_and_fails_closed(
    tmp_path,
):
    from lib.storage_sidecar.fastpath import (
        write_local_manifest,
        write_shadow_manifest,
    )
    from lib.storage_sidecar.offline import (
        SQLiteAuthorityDiscoveryError,
        resolve_readonly_sqlite_authority,
    )

    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    _create_sidecar_schema(data_dir / 'tofu.db')
    local_dir = tmp_path / 'local-front'
    local_dir.mkdir()
    local_db = local_dir / 'tofu.db'
    _create_sidecar_schema(local_db)
    shadow_dir = data_dir / 'fastpath-shadow'
    shadow_dir.mkdir()
    write_shadow_manifest(shadow_dir, {'authority_uuid': 'authority-1'})
    write_local_manifest(local_dir, {
        'authority_uuid': 'authority-1',
        'shadow_dir': str(shadow_dir.resolve()),
    })
    environment = {'TOFU_STORAGE_FASTPATH_DIR': str(local_dir)}

    location = resolve_readonly_sqlite_authority(
        data_dir, environ=environment)
    assert location.path == local_db.resolve()
    assert location.source == 'fastpath_manifest_lineage'

    (local_dir / 'tofu-fastpath.json').unlink()
    with pytest.raises(SQLiteAuthorityDiscoveryError, match='possibly stale'):
        resolve_readonly_sqlite_authority(data_dir, environ=environment)


def test_transcript_marker_surfaces_failed_stream_verdict():
    from debug.inspect_conversation import _message_markers

    marker = _message_markers({
        'role': 'assistant',
        '_turnStatus': 'failed',
        '_turnSettlement': {
            'cause': 'provider_stream_error',
            'streamState': 'premature_close',
        },
    })

    assert 'turn=failed' in marker
    assert 'cause=provider_stream_error' in marker
    assert 'stream=premature_close' in marker


def test_unknown_id_exits_2_with_guidance(tmp_path):
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    result = _run(db_path, 'conv-missing')
    assert result.returncode == 2
    assert 'Not found' in result.stdout
    assert 'check for a typo' in result.stdout


def test_missing_db_exits_1(tmp_path):
    result = _run(tmp_path, 'conv-any')
    assert result.returncode == 1
    assert 'database not found' in result.stderr


def test_invalid_sqlite_exits_cleanly_with_next_step(tmp_path):
    db_path = tmp_path / 'tofu.db'
    db_path.write_text('not a sqlite database', encoding='utf-8')

    result = _run(db_path, 'conv-any')

    assert result.returncode == 1
    assert 'cannot inspect database' in result.stderr
    assert 'serverctl.py doctor' in result.stderr
    assert 'Traceback' not in result.stderr


@pytest.mark.parametrize('args', [('',), ('conv-any', '--logs', '0')])
def test_invalid_input_is_rejected_before_database_scan(tmp_path, args):
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)

    result = _run(db_path, *args)

    assert result.returncode == 2
    assert 'error:' in result.stderr


def test_matching_log_lines_are_credential_redacted(tmp_path, monkeypatch):
    from debug import inspect_conversation as inspector

    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    (log_dir / 'app.log').write_text(
        'conv-safe Authorization: Bearer super-secret-token-value\n',
        encoding='utf-8')
    monkeypatch.setattr(inspector, 'LOG_DIR', log_dir)

    lines = inspector._scan_logs('conv-safe', 50)

    rendered = '\n'.join(lines)
    assert 'super-secret-token-value' not in rendered
    assert 'Authorization: <redacted>' in rendered
