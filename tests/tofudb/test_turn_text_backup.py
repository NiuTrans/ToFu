"""Active text pages and reconnect frames survive the public backup/restore CLI.

Entry point: pytest with TOFUDB_BIN pointing at the matched native artifact.
Dependencies: disposable benchmark daemon and the production Turn facade.
"""
from pathlib import Path
from unittest.mock import patch
import os
import shutil
import subprocess
import uuid

import pytest

from scripts.tofudb_perf_compare import TofuDbEngine, _OWNER_ID
from lib import turn_lifecycle as turns

pytestmark = pytest.mark.api


def test_active_text_backup_restores_replay_and_remains_appendable(tmp_path, monkeypatch):
    configured_binary = os.environ.get('TOFUDB_BIN', '')
    if not configured_binary:
        pytest.skip('Set TOFUDB_BIN to the matched native artifact for backup verification')
    binary = Path(configured_binary).resolve()
    assert binary.is_file()
    monkeypatch.setenv('TOFU_TURN_DELTA_RECORD_MS', '0')
    monkeypatch.setenv('TOFU_TURN_STRUCTURAL_RECORD_MS', '60000')
    engine = TofuDbEngine(binary, authority_dir_parent=tmp_path)
    restored_engine = None
    engine.start()
    directories = list(engine._dirs)
    conv = 'backup-' + uuid.uuid4().hex
    task_id = conv + '-task'
    try:
        with patch('lib.storage.get_storage_client', lambda **kwargs: engine._client):
            pair = turns.create_turn_pair(
                conv, command_id=conv, user_id=_OWNER_ID,
                input_projection={'content': 'backup'}, config={},
                conversation_defaults={'allowCreate': True, 'settings': {}})
            attempt, turn = pair['attempt']['attemptId'], pair['turn']['turnId']
            turns.bind_task(attempt, task_id, user_id=_OWNER_ID)
            task = {'id': task_id, 'convId': conv, '_attemptId': attempt,
                    '_userId': _OWNER_ID, 'status': 'running', 'content': '',
                    'thinking': '', 'segments': [], 'toolRounds': [], 'config': {}}
            for index in range(80):
                chunk = f'{index:03}-汉😀' + 'x' * 8192
                task['content'] += chunk
                event = {'type': 'delta', 'content': chunk}
                assert turns.record_task_event(task, event, task_event={
                    'task_id': task_id, 'sequence': index + 1, 'event': event}) == 'carried'
            before = turns.get_turn(conv, turn, user_id=_OWNER_ID)
            assert before['projection']['content'] == task['content']
            sync_before = engine._client.query('turn.sync.changes', {
                'conversation_id': conv, 'user_id': _OWNER_ID, 'after': 0, 'limit': 1000})
            assert sum(row['type'] == 'attempt.event' for row in sync_before['events']) >= 80
            assert not sync_before['hasMore']
        source = engine._data_dir
        # Preserve only this test's authority through an orderly daemon stop.
        engine._dirs = []
        engine.stop()
        backup, restored = tmp_path / 'backup', tmp_path / 'restored'
        for command in (
                ['backup', '--data-dir', str(source), '--backup-dir', str(backup)],
                ['restore', '--backup-dir', str(backup), '--data-dir', str(restored)]):
            subprocess.run([str(binary), *command], check=True, capture_output=True,
                           text=True, timeout=120)
        restored_engine = TofuDbEngine(binary, authority_dir_parent=tmp_path)
        restored_engine._data_dir = restored
        restored_engine._projection_dir = tmp_path / 'restored-projection'
        restored_engine._launch_serve()
        with patch('lib.storage.get_storage_client', lambda **kwargs: restored_engine._client):
            after = turns.get_turn(conv, turn, user_id=_OWNER_ID)
            assert after['projection'] == before['projection']
            sync_after = restored_engine._client.query('turn.sync.changes', {
                'conversation_id': conv, 'user_id': _OWNER_ID, 'after': 0, 'limit': 1000})
            assert sync_after == sync_before
            rows = restored_engine._client.query('event.list', {
                'task_id': task_id, 'after_sequence': 0, 'limit': 100})
            assert [row['sequence'] for row in rows] == list(range(1, 81))
            assert ''.join(row['event']['content'] for row in rows) == task['content']
            task['content'] += ' after restore😀'
            assert turns.record_task_event(task, {'type': 'delta', 'content': ' after restore😀'})
            task.update(status='done', finishReason='stop')
            assert turns.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
            assert turns.get_turn(conv, turn, user_id=_OWNER_ID)['projection']['content'] == task['content']
    finally:
        if restored_engine is not None:
            restored_engine.stop()
        if engine._process is not None and engine._process.poll() is None:
            engine.stop()
        for directory in directories:
            shutil.rmtree(directory)
