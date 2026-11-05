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
    root = Path(__file__).resolve().parents[2]
    assert '!/debug/inspect_conversation.py' in (
        root / '.gitignore').read_text(encoding='utf-8')
    assert '!debug/inspect_conversation.py' in (
        root / '.dockerignore').read_text(encoding='utf-8')
    assert "'debug/inspect_conversation.py'" in (
        root / 'export.py').read_text(encoding='utf-8')

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
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
            created_at_ms INTEGER NOT NULL,
            archive_scope TEXT NOT NULL DEFAULT ''
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
    environment['TOFU_PERSONAL_STORAGE_BACKEND'] = 'sqlite'
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
    from lib.storage_authority_sql.preflight import ProjectLease

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
        '(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        ('archive-1', 'conv-compact', 1, '[]', summary,
         json.dumps(receipt), 'working_set', 'task-1', 32, 'model',
         147641, 25982, 88, 2, 'working_set', 2, 1787213975000, ''))
    connection.commit()
    connection.close()

    result = _run(db_path, 'conv-compact')

    assert result.returncode == 0, result.stderr
    assert '== Compaction Archives == (1 archive(s), last 20 returned)' in result.stdout
    assert 'Download two skills and improve both MCP tools' in result.stdout
    assert 'objectiveAnchored' in result.stdout


def test_authority_discovery_prefers_manifest_proven_fastpath_and_fails_closed(
    tmp_path,
):
    from lib.storage_authority_sql.fastpath import (
        write_local_manifest,
        write_shadow_manifest,
    )
    from lib.storage_authority_sql.offline import (
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


def _log_text(sections) -> str:
    return '\n'.join(line for header, matched in sections
                     for line in (header, *(matched or ())))


def test_matching_log_lines_preserve_diagnostic_values(tmp_path, monkeypatch):
    from debug import inspect_conversation as inspector

    log_dir = tmp_path / 'logs'
    log_dir.mkdir()
    (log_dir / 'app.log').write_text(
        'conv-safe Authorization: Bearer super-secret-token-value\n',
        encoding='utf-8')
    monkeypatch.setattr(inspector, 'LOG_DIR', log_dir)

    rendered = _log_text(inspector._scan_logs('conv-safe', 50))
    # LOGGING.md requires original local diagnostic values with bounded output.
    assert 'Authorization: Bearer super-secret-token-value' in rendered
    assert '<redacted>' not in rendered


def test_failed_turn_anomaly_leads_with_the_typed_error_detail():
    """The localized message only says "HTTP 400"; the typed detail names the
    cause (2026-10-31: invalid_encrypted_content hid behind that message)."""
    from debug.inspect_conversation import _turn_anomaly

    record = _turn_anomaly({'role': 'assistant', '_turnStatus': 'failed',
                            '_turnSettlement': {
        'cause': 'generation_error', 'outcome': 'failed',
        'error': {'kind': 'bad_request', 'statusCode': 400,
                  'message': 'Request rejected by the API (HTTP 400)',
                  'detail': 'Responses encrypted state rejected '
                            '(invalid_encrypted_content)'}}})

    error = record['error']
    assert 'invalid_encrypted_content' in error
    assert error.index('invalid_encrypted_content') < error.index('HTTP 400')


def test_logs_include_rotations_written_during_the_conversation(tmp_path):
    import os

    from debug import inspect_conversation as inspector

    created, updated = 1_000_000.0, 1_005_000.0
    files = {
        'app.log.2026-10-30': ('conv-x before the conversation\n', created - 10),
        'app.log.2026-10-31': ('conv-x round 27 ok\n', created + 100),
        'app.log.2026-10-31.00001': ('conv-x round 28 rejected\n', updated + 50),
        'app.log.2026-11-01': ('conv-x after the conversation\n', updated + 900),
        'app.log': ('conv-x inspector run\n', updated + 9_000),
        'app.log.lock': ('', updated),
    }
    for name, (text, mtime) in files.items():
        path = tmp_path / name
        path.write_text(text, encoding='utf-8')
        os.utime(path, (mtime, mtime))

    rendered = _log_text(inspector._scan_logs(
        'conv-x', 50, tmp_path, window=(created, updated)))

    assert 'round 27 ok' in rendered and 'round 28 rejected' in rendered
    assert 'before the conversation' not in rendered
    assert 'after the conversation' not in rendered
    assert '--- app.log.2026-10-31: 1 line(s)' in rendered
    assert 'inspector run' in rendered, 'the live tail is always read'
    assert 'round 28' not in _log_text(inspector._scan_logs('conv-x', 50, tmp_path))


def _write_rotation_and_tail(directory, text, mtime):
    rotated = directory / 'app.log.2026-11-02'
    rotated.write_text(text, encoding='utf-8')
    os.utime(rotated, (mtime, mtime))
    (directory / 'app.log').write_text(text, encoding='utf-8')


def test_log_scan_matches_short_id_tokens_but_not_a_shared_prefix(tmp_path):
    """Model-replay/translation call sites log ``conversation_id[:8]``."""
    from debug import inspect_conversation as inspector

    full = 'GET /api/v4/conversations/mvyt8wab9nt9fa 200'
    short = '[ModelReplay] conv=mvyt8wab restore=x'
    _write_rotation_and_tail(tmp_path, '\n'.join([
        full, short,
        '[ModelReplay] conv=mvyt8wabzzzzzz restore=another-conversation',
        '[Translate] committed conv=xmvyt8wab turn=embedded',
        '[Chat] unrelated line',
    ]) + '\n', 1_000_100.0)

    sections = inspector._scan_logs('mvyt8wab9nt9fa', 50, tmp_path,
                                    window=(1_000_000.0, 1_005_000.0))

    assert sections == [
        ('--- app.log.2026-11-02: 2 line(s) '
         '(rotation overlapping the conversation lifetime)', [full, short]),
        ('--- app.log: 2 line(s) (last 4 MiB)', [full, short]),
    ]


def test_log_scan_requires_a_standalone_token_for_ids_up_to_the_short_length(tmp_path):
    from debug import inspect_conversation as inspector

    (tmp_path / 'app.log').write_text(
        'conv-x round 1 ok\nconv-xyz other conversation\nid=conv-x.\n', encoding='utf-8')

    assert inspector._scan_logs('conv-x', 50, tmp_path) == [
        ('--- app.log: 2 line(s) (last 4 MiB)', ['conv-x round 1 ok', 'id=conv-x.'])]


def test_log_match_filters_conversation_lines_before_the_per_file_cap(tmp_path):
    import re

    from debug import inspect_conversation as inspector

    lines = [f'[ModelReplay] conv=mvyt8wab restore=r{number}' if number % 2
             else f'[Chat] conv=mvyt8wab noise {number}' for number in range(10)]
    lines.append('[ModelReplay] conv=other123 restore=not-this-conversation')
    _write_rotation_and_tail(tmp_path, '\n'.join(lines) + '\n', 1_000_100.0)

    sections = inspector._scan_logs(
        'mvyt8wab9nt9fa', 2, tmp_path, window=(1_000_000.0, 1_005_000.0),
        log_match=re.compile(r'ModelReplay|Translate\] committed'))

    kept = ['[ModelReplay] conv=mvyt8wab restore=r7', '[ModelReplay] conv=mvyt8wab restore=r9']
    assert sections == [
        ('--- app.log.2026-11-02: 2 line(s) '
         '(rotation overlapping the conversation lifetime)', kept),
        ('--- app.log: 2 line(s) (last 4 MiB)', kept),
    ]


@pytest.mark.parametrize('argv,message', [
    (['--log-match', '('], 'invalid regular expression'),
    (['--log-match', 'x', '--no-logs'], '--log-match and --no-logs are contradictory'),
    (['--json', '--raw'], '--json already carries the full messages array'),
])
def test_new_flag_misuse_is_a_usage_error(argv, message, capsys):
    from debug import inspect_conversation as inspector

    with pytest.raises(SystemExit) as error:
        inspector.main(['conv-any', '--db', 'unused.db', *argv])

    assert error.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err and captured.out == ''


def test_json_mode_prints_exactly_one_document(tmp_path, capsys):
    from debug import inspect_conversation as inspector

    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-json')

    assert inspector.main(['conv-json', '--db', str(db_path), '--no-logs', '--json']) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.out.count('\n') == 1 and not captured.err
    assert set(payload) == {'conversationId', 'found', 'exitCode', 'document',
                            'messages', 'anomalies', 'compactionArchives'}
    assert (payload['conversationId'], payload['found'], payload['exitCode']) == (
        'conv-json', True, 0)
    assert 'messages' not in payload['document']
    assert payload['document']['metadata']['title'] == 'turn title'
    assert [message['content'] for message in payload['messages']] == ['hello turns']
    assert payload['anomalies'] == [] and payload['compactionArchives'] == []

    assert inspector.main(['conv-missing', '--db', str(db_path), '--no-logs', '--json']) == 2
    assert json.loads(capsys.readouterr().out) == {
        'conversationId': 'conv-missing', 'found': False, 'exitCode': 2}

    assert inspector.main(['conv-json', '--db', str(tmp_path), '--no-logs', '--json']) == 1
    captured = capsys.readouterr()
    assert captured.out == '' and 'database not found' in captured.err


def test_json_cli_stdout_is_one_document_without_report_text(tmp_path):
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-json')

    result = _run(db_path, 'conv-json', '--json', '--context')

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['contextInjections'] == []
    assert '== ' not in result.stdout


def test_context_injections_list_rule_notes_with_sources_and_repeats():
    from debug.inspect_conversation import _context_injections, _render_context_injections

    rule = ('Project Intelligence — AGENTS.md\nSource: /repo/AGENTS.md\n'
            'Applies only within: /repo\n'
            'Nested AGENTS.md specializes its subtree; guidance does not grant access.\n\n'
            '# Rules\n- keep it small\n')
    note = {'type': 'system_note', 'noteKind': 'system-intervention',
            'reason': 'project_scoped_rules', 'text': rule}
    document = {'messages': [
        {'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'segments': [
            {**note, 'llmRound': 0}, {'type': 'text', 'text': 'answer'}]},
        {'role': 'assistant', 'segments': [
            {'type': 'system_note', 'noteKind': 'intent-stall', 'llmRound': 2,
             'text': 'Continue the plan.'},
            {**note, 'llmRound': 3},
            {**note, 'llmRound': None, 'text': (
                'Source: /repo/sub/AGENTS.md — same bytes as /repo/AGENTS.md; that text '
                'applies within /repo/sub too and is not repeated here.')}]},
    ]}
    before = json.dumps(document, sort_keys=True)

    records = _context_injections(document)

    assert json.dumps(document, sort_keys=True) == before
    assert records[2]['repeatOf'] == {'messageNumber': 2, 'llmRound': 0}
    assert records[0]['sha256'] == records[2]['sha256'] != records[1]['sha256']
    assert _render_context_injections(records) == [
        '  #2 r0 project_scoped_rules: 6 line(s) source: /repo/AGENTS.md',
        '  #3 r2 intent-stall: 1 line(s)',
        '  #3 r3 project_scoped_rules: 6 line(s) source: /repo/AGENTS.md (repeat of #2 r0)',
        '  #3 r? project_scoped_rules: 1 line(s) source: /repo/sub/AGENTS.md',
        '  4 note(s), 3 distinct text(s), 1 repeat(s)',
    ]


@pytest.mark.parametrize('backend', ['unknown-backend'])
def test_auto_discovery_refuses_non_sqlite_project_configuration(tmp_path, monkeypatch, capsys, backend):
    from debug import inspect_conversation as inspector
    monkeypatch.setattr(inspector, 'REPO_ROOT', tmp_path)
    (tmp_path / '.env').write_text(f'TOFU_PERSONAL_STORAGE_BACKEND={backend}\n')
    monkeypatch.delenv('TOFU_PERSONAL_STORAGE_BACKEND', raising=False)
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    # Isolate from the developer checkout's PORT: an unreadable configuration
    # is reported as a configuration error without probing either authority.
    monkeypatch.setenv('PORT', '1')
    def forbidden(*args, **kwargs):
        pytest.fail('must not resolve a leftover SQLite database for another backend')
    monkeypatch.setattr(inspector, 'resolve_readonly_sqlite_authority', forbidden)
    assert inspector.main(['conv-owner', '--no-logs']) == 1
    assert 'configuration' in capsys.readouterr().err.lower()


def test_tofudb_backend_auto_discovers_application_api(tmp_path, monkeypatch, capsys):
    """A tofudb deployment has no SQLite file: the inspector must route the
    bare invocation through the running application's v4 API instead of
    refusing."""
    from debug import inspect_conversation as inspector
    monkeypatch.setattr(inspector, 'REPO_ROOT', tmp_path)
    (tmp_path / '.env').write_text(
        'TOFU_PERSONAL_STORAGE_BACKEND=tofudb\nPORT=15678\n')
    for name in ('TOFU_PERSONAL_STORAGE_BACKEND', 'PORT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    monkeypatch.setattr(
        inspector, 'resolve_readonly_sqlite_authority',
        lambda *a, **k: pytest.fail('tofudb must not open SQLite'))
    calls = []
    class FakeAPI:
        def __init__(self, url, token):
            calls.append(('init', url, token))
        @property
        def origin(self):
            return 'http://127.0.0.1:15678'
    def fake_load(api, conv_id, *, full, start_index=None, max_turns=None):
        assert start_index is None and max_turns is None
        calls.append(('load', conv_id, full))
        return {
            'source': 'authenticated-application-api',
            'metadata': {'id': conv_id, 'title': 'native document'},
            'messages': [{'role': 'user', 'content': 'hi'}],
            'diagnostic_archive_catalog_count': 0,
        }, []
    monkeypatch.setattr(inspector, '_DiagnosticAPI', FakeAPI)
    monkeypatch.setattr(inspector, '_load_api_document', fake_load)
    assert inspector.main(['conv-native', '--no-logs']) == 0
    assert calls == [
        ('init', 'http://127.0.0.1:15678', None),
        ('load', 'conv-native', False),
    ]
    out = capsys.readouterr().out
    assert 'auto-discovered application API' in out
    assert 'native document' in out


def test_tofudb_auto_api_honors_explicit_environment_port(tmp_path, monkeypatch):
    from debug import inspect_conversation as inspector
    monkeypatch.setattr(inspector, 'REPO_ROOT', tmp_path)
    (tmp_path / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\n')
    monkeypatch.delenv('TOFU_PERSONAL_STORAGE_BACKEND', raising=False)
    monkeypatch.setenv('PORT', '17001')
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    urls = []
    class FakeAPI:
        def __init__(self, url, token):
            urls.append(url)
        @property
        def origin(self):
            return urls[0]
    monkeypatch.setattr(inspector, '_DiagnosticAPI', FakeAPI)
    monkeypatch.setattr(
        inspector, '_load_api_document', lambda *a, **k: (None, []))
    assert inspector.main(['conv-native', '--no-logs']) == 2
    assert urls == ['http://127.0.0.1:17001']


def test_sqlite_environment_override_beats_project_backend(tmp_path, monkeypatch, capsys):
    from debug import inspect_conversation as inspector
    from lib.storage_authority_sql.offline import SQLiteAuthorityDiscoveryError
    monkeypatch.setattr(inspector, 'REPO_ROOT', tmp_path)
    (tmp_path / '.env').write_text('TOFU_PERSONAL_STORAGE_BACKEND=tofudb\n')
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', 'sqlite')
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    called=[]
    def discover(*args, **kwargs):
        called.append(True)
        raise SQLiteAuthorityDiscoveryError('expected fixture discovery')
    monkeypatch.setattr(inspector, 'resolve_readonly_sqlite_authority', discover)
    assert inspector.main(['conv-owner', '--no-logs']) == 1
    assert called == [True]
    assert 'expected fixture discovery' in capsys.readouterr().err


def test_auto_discovery_uses_dotenv_data_root_without_creating_it(tmp_path, monkeypatch, capsys):
    from debug import inspect_conversation as inspector
    monkeypatch.setattr(inspector, 'REPO_ROOT', tmp_path)
    target = tmp_path / 'relocated'
    (tmp_path / '.env').write_text(
        f'TOFU_PERSONAL_STORAGE_BACKEND=sqlite\nTOFU_DATA_DIR={target}\n')
    monkeypatch.delenv('TOFU_DATA_DIR', raising=False)
    monkeypatch.setenv('TOFU_PERSONAL_STORAGE_BACKEND', 'sqlite')
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
    monkeypatch.setenv('TOFU_PROCESS_ROLE', 'all')
    discovered=[]
    def discover(data_dir, **kwargs):
        discovered.append(Path(data_dir))
        assert kwargs['environ']['TOFU_DATA_DIR'] == str(target)
        raise FileNotFoundError('expected missing authority')
    monkeypatch.setattr(inspector, 'resolve_readonly_sqlite_authority', discover)
    assert inspector.main(['conv-owner', '--no-logs']) == 1
    assert discovered == [target / 'data']
    assert not target.exists(), 'read-only diagnostic created runtime directories'


def test_tool_round_anomaly_flags_rejection_and_skips_healthy():
    from debug.inspect_conversation import _tool_round_anomaly

    rejected = _tool_round_anomaly({
        'status': 'rejected', 'toolName': 'write_file', 'roundNum': 3,
        '_contractError': {'code': 'schema', 'message': 'path required',
                           'path': '/x', 'nextAction': 'fix args'},
        'toolArgs': {'bad': 1}, 'taskId': 'task-abcdefghijkl'})
    assert rejected['rejection'] is True
    assert rejected['tool'] == 'write_file'
    assert rejected['round'] == 3
    assert 'path required' in rejected['error']
    assert 'fix args' in rejected['error']

    errored = _tool_round_anomaly({
        'status': 'error', 'toolName': 'run_command', 'roundNum': 4,
        'result': {'error': 'exit 1'}})
    assert errored['rejection'] is False
    assert 'exit 1' in errored['error']

    assert _tool_round_anomaly({'status': 'done', 'toolName': 'grep_search'}) is None
    # Content-free orphan rounds carry no debugging signal.
    assert _tool_round_anomaly({'status': '', 'toolName': '', 'query': ''}) is None


@pytest.mark.parametrize('carrier', ['result', 'results', 'tool_use', 'tool_gateway',
                                    'toolContent', 'content_blocks'])
def test_done_tool_round_reports_structured_result_failure(carrier):
    from debug.inspect_conversation import _load_anomalies

    failure = {'status': 'error', 'error': {'code': 'tool_failed', 'message': 'synthetic failure'}}
    entry = {'status': 'done', 'toolName': 'read_files', 'roundNum': 4,
             'toolCallId': 'call-1', 'toolArgs': '{"path":"a.py"}',
             'taskId': 'task-abcdefgh'}
    if carrier == 'result':
        entry['result'] = failure
    elif carrier == 'results':
        entry['results'] = [{'toolName': 'read_files', **failure}]
    elif carrier == 'tool_use':
        entry['results'] = [{'type': 'tool_use', 'toolName': 'read_files',
                             'result': {'status': 'error', 'content': json.dumps(failure)}}]
    elif carrier == 'tool_gateway':
        entry['toolName'] = 'execute_tools'
        entry['results'] = [{'type': 'tool_gateway', 'toolName': 'execute_tools',
                             'content': json.dumps({'status': 'ok', 'results': [
                                 {'call_id': 'child-1', 'name': 'read_files', 'status': 'done',
                                  'output': json.dumps(failure)}]})}]
    elif carrier == 'content_blocks':
        entry['result'] = {'content': [{'type': 'text', 'text': json.dumps(failure)}]}
    else:
        entry['toolContent'] = json.dumps(failure)
    document = {'messages': [{'role': 'assistant', 'toolRounds': [entry]}]}
    before = json.dumps(document, sort_keys=True)

    anomalies = _load_anomalies(None, 'conv-synthetic', document)

    assert len(anomalies) == 1
    assert anomalies[0]['tool'] == 'read_files'
    assert anomalies[0]['status'] == 'error'
    assert anomalies[0]['rejection'] is False
    assert anomalies[0]['round'] == 4
    assert anomalies[0]['taskId'] == 'task-abc'
    assert 'synthetic failure' in anomalies[0]['error']
    assert json.dumps(document, sort_keys=True) == before, 'diagnostic mutated its source'


def test_done_gateway_reports_each_failed_child_without_duplicate_surfaces():
    from debug.inspect_conversation import _load_anomalies, _render_anomalies

    payload = {'status': 'partial_failure', 'results': [
        {'call_id': 'healthy', 'name': 'grep_search', 'status': 'done',
         'output': 'error handling documentation'},
        {'call_id': 'unknown', 'name': 'run_command', 'status': 'unknown',
         'outcome': 'unknown', 'error': 'delivery outcome unknown'},
        {'call_id': 'failed', 'name': 'read_files', 'status': 'error',
         'error': {'code': 'io', 'message': 'synthetic unreadable file'}},
        {'call_id': 'rejected', 'name': 'edit_file', 'status': 'rejected',
         'error': json.dumps({'status': 'error', 'message': 'synthetic stale base'})},
    ]}
    content = json.dumps(payload)
    document = {'messages': [{'role': 'assistant', 'toolRounds': [{
        'roundNum': 7, 'status': 'done', 'toolName': 'execute_tools',
        'toolCallId': 'gateway-call', 'taskId': 'task-abcdefgh',
        'toolArgs': {'calls': [{'name': 'read_files', 'arguments': {'path': 'a.py'}}]},
        'toolContent': content,
        'results': [{'type': 'tool_gateway', 'toolName': 'execute_tools', 'content': content}],
    }]}]}

    anomalies = _load_anomalies(None, 'conv-synthetic', document)

    assert [(item['tool'], item['status'], item['rejection']) for item in anomalies] == [
        ('edit_file', 'rejected', True), ('read_files', 'error', False)]
    assert all(item['round'] == 7 and item['taskId'] == 'task-abc' for item in anomalies)
    assert all(not item['args'] for item in anomalies), 'gateway args are not child args'
    rendered = '\n'.join(_render_anomalies(anomalies, full=False))
    assert 'synthetic stale base' in rendered and 'synthetic unreadable file' in rendered
    assert 'status=done' not in rendered and 'run_command' not in rendered


@pytest.mark.parametrize('call_id', [None, 'recycled-child-id'])
def test_identical_failed_siblings_are_not_dropped_as_duplicate_surfaces(call_id):
    from debug.inspect_conversation import _load_anomalies

    failure = {'name': 'read_files', 'status': 'error', 'error': 'same failure'}
    if call_id is not None:
        failure['call_id'] = call_id
    content = json.dumps({'status': 'partial_failure', 'results': [failure, failure]})
    entry = {'status': 'done', 'toolName': 'execute_tools', 'toolContent': content,
             'results': [{'toolName': 'execute_tools', 'content': content}]}

    anomalies = _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]})

    assert len(anomalies) == 2
    assert all(item['tool'] == 'read_files' and item['status'] == 'error' for item in anomalies)


@pytest.mark.parametrize('status', ['done', 'searching', 'executing', 'pending_approval',
                                    'running', 'partial', 'unknown'])
def test_tool_round_anomalies_do_not_infer_failure_from_prose_or_arguments(status):
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'role': 'assistant', 'toolRounds': [{
        'status': status, 'toolName': 'read_files',
        'toolArgs': {'status': 'error', 'error': 'input fixture, not an outcome'},
        'thinking': 'error rejected failed', '_rejected': False,
        'results': [{'toolName': 'read_files', 'status': status,
                     'snippet': '{"status":"error"}',
                     'content': json.dumps({'summary': 'error/rejected in documentation',
                                            'data': {'status': 'error'}})}],
    }]}]}

    assert _load_anomalies(None, 'conv-synthetic', document) == []


@pytest.mark.parametrize('carrier', ['injection', 'message', 'tool_result'])
def test_background_command_completion_reports_typed_nonzero_exit(carrier):
    from debug.inspect_conversation import _load_anomalies, _render_anomalies

    completion = {'commandId': 'bg_synthetic', 'status': 'completed',
                  'command': 'synthetic-command', 'exitCode': 2,
                  'resultPreview': 'synthetic command failed', 'resultTruncated': False}
    message = {'role': 'assistant', '_turnId': 'turn-synthetic'}
    if carrier == 'injection':
        message['_bgCommandInjects'] = [{'round': 3, 'count': 1, 'previews': [
            {'commandId': 'bg_synthetic', 'backgroundCommand': completion}]}]
    elif carrier == 'message':
        message['backgroundCommand'] = completion
    else:
        message['toolRounds'] = [{'status': 'done', 'toolName': 'poll',
                                 'results': [completion]}]
    document = {'messages': [message]}
    before = json.dumps(document, sort_keys=True)

    anomalies = _load_anomalies(None, 'conv-synthetic', document)

    assert len(anomalies) == 1
    assert anomalies[0]['rejection'] is False
    assert anomalies[0]['commandId'] == 'bg_synthetic'
    rendered = '\n'.join(_render_anomalies(anomalies, full=False))
    assert 'bg_synthetic' in rendered and 'exitCode=2' in rendered
    assert json.dumps(document, sort_keys=True) == before


@pytest.mark.parametrize('exit_code', [0, None, 'background', 'unknown', '2', True])
def test_background_completion_without_typed_failure_is_not_an_anomaly(exit_code):
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'_bgCommandInjects': [{'previews': [{'backgroundCommand': {
        'commandId': 'bg_synthetic', 'status': 'completed', 'exitCode': exit_code,
        'resultPreview': 'Error: illustrative prose [exit code: 2]',
    }}]}], 'toolRounds': [{'status': 'done', 'toolName': 'poll', 'toolContent':
                           '== bg_synthetic (background command) ==\n[exit code: 2]'}]}]}
    assert _load_anomalies(None, 'conv-synthetic', document) == []


@pytest.mark.parametrize('status', ['running', 'pending_approval', 'unknown'])
def test_background_unfinished_command_is_not_an_anomaly(status):
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'backgroundCommand': {
        'commandId': 'bg_synthetic', 'status': status, 'exitCode': 2}}]}
    assert _load_anomalies(None, 'conv-synthetic', document) == []


@pytest.mark.parametrize('exit_code', ['1', '-1', 2])
def test_done_run_command_frontend_result_reports_nonzero_exit(exit_code):
    from debug.inspect_conversation import _load_anomalies, _render_anomalies

    entry = {'status': 'done', 'toolName': 'run_command', 'roundNum': 9,
             'taskId': 'task-synthetic', 'results': [{
                 'badge': f'exit {exit_code}', 'command': 'synthetic-command',
                 'description': 'synthetic fixture', 'exitCode': exit_code,
                 'fetched': True, 'fetchedChars': 23, 'interrupted': False,
                 'output': 'synthetic foreground failure', 'snippet': '$ synthetic-command',
                 'source': 'Project', 'timedOut': False, 'title': 'Run command', 'url': '',
             }]}
    document = {'messages': [{'role': 'assistant', 'toolRounds': [entry]}]}
    before = json.dumps(document, sort_keys=True)

    anomalies = _load_anomalies(None, 'conv-synthetic', document)

    assert len(anomalies) == 1 and anomalies[0]['tool'] == 'run_command'
    assert anomalies[0]['status'] == 'error' and anomalies[0]['rejection'] is False
    assert 'exitCode=' + str(exit_code) in '\n'.join(_render_anomalies(anomalies, full=False))
    assert 'synthetic foreground failure' in anomalies[0]['error']
    assert json.dumps(document, sort_keys=True) == before


@pytest.mark.parametrize('field,status', [('timedOut', 'timeout'), ('interrupted', 'aborted')])
def test_done_run_command_frontend_result_reports_typed_terminal_flags(field, status):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': 'done', 'toolName': 'run_command',
             'results': [{'exitCode': 'unknown', field: True, 'command': 'synthetic-command'}]}
    anomalies = _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]})
    assert len(anomalies) == 1 and anomalies[0]['status'] == status
    assert anomalies[0]['rejection'] is False


@pytest.mark.parametrize('exit_code', ['0', 0, 'background', 'unknown', '1x', ' 1',
                                       '1.0', '１', '1' * 11, True, None])
def test_run_command_frontend_nonfailure_or_invalid_exit_is_neutral(exit_code):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': 'done', 'toolName': 'run_command', 'results': [{
        'exitCode': exit_code, 'timedOut': False, 'interrupted': False,
        'output': '{"exitCode":"1","message":"error word in data"}', 'badge': 'exit 1'}]}
    assert _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]}) == []


@pytest.mark.parametrize('status', ['running', 'searching', 'executing', 'pending_approval', 'unknown'])
def test_run_command_unfinished_frontend_result_does_not_become_failure(status):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': status, 'toolName': 'run_command', 'results': [{'exitCode': '1'}]}
    assert _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]}) == []


@pytest.mark.parametrize('tool', ['read_files', 'poll', 'execute_tools'])
def test_noncommand_data_exitcode_is_not_foreground_execution_evidence(tool):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': 'done', 'toolName': tool,
             'results': [{'exitCode': '1', 'timedOut': True, 'data': {'exitCode': 1}}],
             'toolContent': '{"data":{"exitCode":"1"},"exitCode":"1"}'}
    assert _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]}) == []


@pytest.mark.parametrize('result', [
    {'toolName': 'read_files', 'exitCode': '1'},
    {'status': 'running', 'exitCode': '1'},
    {'status': 'unknown', 'exitCode': '1'},
])
def test_run_command_parent_done_does_not_override_other_tool_or_child_state(result):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': 'done', 'toolName': 'run_command', 'results': [result]}
    assert _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]}) == []


def test_run_command_exitcode_in_body_data_is_not_frontend_metadata():
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': 'done', 'toolName': 'run_command',
             'toolContent': '{"exitCode":"1","timedOut":true}',
             'results': [{'content': '{"data":{"exitCode":"1"},"exitCode":"1"}'}]}
    assert _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]}) == []


def test_background_completion_duplicate_receipts_count_once():
    from debug.inspect_conversation import _load_anomalies

    completion = {'commandId': 'bg_synthetic', 'status': 'completed', 'exitCode': -1}
    message = {'backgroundCommand': completion, '_bgCommandInjects': [
        {'previews': [{'backgroundCommand': completion}, {'backgroundCommand': completion}]}]}
    assert len(_load_anomalies(None, 'conv-synthetic', {'messages': [message]})) == 1


@pytest.mark.parametrize('status', ['completed', 'running', 'pending', None])
def test_turn_settlement_unknown_or_error_text_is_not_a_failure(status):
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'_turnStatus': status, '_turnSettlement': {
        'outcome': 'unknown', 'error': {'message': 'error in illustrative evidence'},
        'cause': 'provider_stream_error', 'streamState': 'unknown'}}]}
    assert _load_anomalies(None, 'conv-synthetic', document) == []


def test_tool_result_anomaly_json_scan_is_size_node_and_depth_bounded(monkeypatch):
    from debug import inspect_conversation as inspector

    monkeypatch.setattr(inspector, 'ANOMALY_JSON_CHAR_CAP', 128)
    monkeypatch.setattr(inspector, 'ANOMALY_JSON_TOTAL_CHAR_CAP', 256)
    monkeypatch.setattr(inspector, 'ANOMALY_NODE_CAP', 16)
    monkeypatch.setattr(inspector, 'ANOMALY_DEPTH_CAP', 6)
    original_loads = json.loads
    decoded_sizes = []
    def bounded_loads(raw, *args, **kwargs):
        decoded_sizes.append(len(raw))
        assert len(raw) <= 128
        return original_loads(raw, *args, **kwargs)
    monkeypatch.setattr(inspector.json, 'loads', bounded_loads)
    oversized = json.dumps({'status': 'error', 'message': 'x' * 128})
    nested = {'status': 'error', 'message': 'too deep'}
    for _ in range(20):
        nested = {'result': nested}
    cases = [oversized, nested, ['not JSON'] * 30 + [{'status': 'error'}],
             '{"status":"error",', '[' * 200 + ']' * 200]
    for content in cases:
        document = {'messages': [{'toolRounds': [{'status': 'done', 'toolName': 'read_files',
                                                'result': content}]}]}
        assert inspector._load_anomalies(None, 'conv-synthetic', document) == []
    assert all(size <= 128 for size in decoded_sizes)


def test_tool_result_malformed_carriers_do_not_abort_diagnostic():
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [None, {'toolRounds': 7}, {'toolRounds': [None, {
        'status': 'done', 'toolName': 'read_files', 'results': {'unexpected': 'error'},
        'toolContent': '{"status":"error"', 'result': [None, 7, 'error rejected'],
    }]}]}
    assert _load_anomalies(None, 'conv-synthetic', document) == []


def test_text_content_blocks_and_status_only_display_are_one_result():
    from debug.inspect_conversation import _load_anomalies

    failure = json.dumps({'status': 'error', 'message': 'detailed failure'})
    entry = {'status': 'done', 'toolName': 'read_files', 'toolCallId': 'call-1',
             'results': [{'toolName': 'read_files', 'status': 'error'}],
             'result': {'content': [{'type': 'text', 'text': 'header only'},
                                    {'type': 'text', 'text': failure}]}}
    anomalies = _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]})
    assert len(anomalies) == 1 and 'detailed failure' in anomalies[0]['error']


@pytest.mark.parametrize('field,status', [('timedOut', 'timeout'), ('interrupted', 'aborted')])
def test_background_completed_typed_timeout_or_interrupt_reports_failure(field, status):
    from debug.inspect_conversation import _load_anomalies

    completion = {'commandId': 'bg_synthetic', 'status': 'completed', field: True}
    anomalies = _load_anomalies(None, 'conv-synthetic',
                                {'messages': [{'backgroundCommand': completion}]})
    assert len(anomalies) == 1 and anomalies[0]['status'] == status
    assert anomalies[0]['rejection'] is False


def test_tool_result_anomaly_aggregate_json_decode_budget(monkeypatch):
    from debug import inspect_conversation as inspector

    healthy = json.dumps({'status': 'done', 'output': 'healthy'})
    failure = json.dumps({'status': 'error', 'message': 'beyond aggregate budget'})
    monkeypatch.setattr(inspector, 'ANOMALY_JSON_TOTAL_CHAR_CAP', len(healthy) * 2)
    entry = {'status': 'done', 'toolName': 'read_files',
             'results': [{'content': healthy}, {'content': healthy}, {'content': failure}]}

    assert inspector._load_anomalies(None, 'conv-synthetic',
                                     {'messages': [{'toolRounds': [entry]}]}) == []


@pytest.mark.parametrize('outer', ['error', 'done'])
def test_explicit_rejection_wins_over_nested_error_detail(outer):
    from debug.inspect_conversation import _load_anomalies

    entry = {'status': outer, 'toolName': 'edit_file',
             'result': {'status': 'rejected', 'content':
                        json.dumps({'status': 'error', 'message': 'stale version'})}}
    anomalies = _load_anomalies(None, 'conv-synthetic', {'messages': [{'toolRounds': [entry]}]})
    assert len(anomalies) == 1 and anomalies[0]['rejection'] is True
    assert anomalies[0]['status'] == 'rejected' and 'stale version' in anomalies[0]['error']


def test_unknown_effects_error_is_not_a_rejection():
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'toolRounds': [{
        'status': 'done', 'toolName': 'run_command',
        'result': {'status': 'error', 'outcome': 'unknown',
                   'error': {'code': 'outcome_unknown', 'message': 'effects may have happened'}},
    }]}]}

    anomalies = _load_anomalies(None, 'conv-synthetic', document)

    assert len(anomalies) == 1 and anomalies[0]['status'] == 'error'
    assert anomalies[0]['rejection'] is False


def test_load_anomalies_sorts_rejections_first_and_reads_attempt_errors():
    from debug.inspect_conversation import _load_anomalies

    document = {'messages': [{'role': 'assistant', 'toolRounds': [
        {'status': 'error', 'toolName': 'read_files', 'roundNum': 2,
         'result': {'error': 'io'}},
        {'status': 'rejected', 'toolName': 'edit_file', 'roundNum': 1,
         '_rejected': {'reason': 'stale base'}, 'toolArgs': {'path': 'a'}},
    ]}]}
    anomalies = _load_anomalies(None, 'conv-x', document)
    assert [a['tool'] for a in anomalies] == ['edit_file', 'read_files']
    assert anomalies[0]['rejection'] is True


def test_render_tool_rounds_and_anomalies_sections():
    from debug.inspect_conversation import _render_anomalies, _render_tool_rounds

    lines = _render_tool_rounds({'toolRounds': [{
        'roundNum': 2, 'status': 'error', 'thinking': 'why it failed',
        'results': [{'toolName': 'grep_search', 'status': 'error',
                     'error': 'pattern too broad'}]}]}, full=False)
    assert any('tool round 2: grep_search' in line for line in lines)
    assert any('thinking:' in line for line in lines)
    assert any('pattern too broad' in line for line in lines)

    rendered = _render_anomalies([{
        'kind': 'tool', 'tool': 'write_file', 'status': 'rejected',
        'rejection': True, 'round': 1, 'args': '{}', 'error': 'schema',
        'taskId': 't1'}], full=False)
    assert any('⚑ REJECTED' in line for line in rendered)
    assert _render_anomalies([], full=False) == ['<no tool/attempt anomalies>']


def test_main_renders_anomalies_section_for_turn_conversation(tmp_path):
    db_path = tmp_path / 'tofu.db'
    _create_sidecar_schema(db_path)
    _insert_turn_native_conversation(db_path, 'conv-anomaly')
    result = _run(db_path, 'conv-anomaly')
    assert result.returncode == 0, result.stderr
    assert '== Anomalies ==' in result.stdout
    assert 'no tool/attempt anomalies' in result.stdout
    suppressed = _run(db_path, 'conv-anomaly', '--no-anomalies')
    assert suppressed.returncode == 0, suppressed.stderr
    assert '== Anomalies ==' not in suppressed.stdout
