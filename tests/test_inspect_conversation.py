"""Guard tests for debug/inspect_conversation.py.

The script is the documented first step for conversation-ID debugging
(AGENTS.md). These tests pin its contract against a disposable sqlite file:
location probes, turn-native projection, rendering, and the not-found exit
code.
"""

import json
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
