"""File-change commands answer 400 only for the file-history engine's refusals.

The real route, command service, runtime gateway and file-history engine run
over the SQLite Sidecar. A refusal keeps its authored message as the 400 body
and as the turn's durable ``fileChanges.error``. An engine fault is a logged,
redacted 500; the durable error names the file but carries no raw exception
text, which stays in the server log.
"""

from __future__ import annotations

import logging
from pathlib import Path
import uuid

import pytest

pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture
def file_turn(chat_sidecar, flask_client, tmp_path, monkeypatch):
    """A settled assistant turn whose task really wrote ``notes.txt``."""
    from lib import file_history
    from lib.project_mod import config as pm_config
    from lib.project_mod.write_tools import tool_write_file
    from lib.turn_lifecycle import apply_commit_round_file_changes
    from tests._seed import delete_conversation, seed_conversation

    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    conversation = f'conv-file-fault-{uuid.uuid4().hex[:12]}'
    task = f'task-file-fault-{uuid.uuid4().hex}'
    root = tmp_path / 'project'
    root.mkdir()
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._roots['project'] = pm_config._make_root_state(str(root))
        pm_config._state['path'] = str(root)
    pm_config.set_conv_roots(conversation, str(root))
    seed_conversation(conversation, title='File change faults', messages=[])
    try:
        (root / 'notes.txt').write_text('original\n')
        written = tool_write_file(str(root), 'notes.txt', 'assistant edit\n',
                                  conv_id=conversation, task_id=task)
        assert written['ok'], written
        assert file_history.make_snapshot(
            str(root), task_id=task, conv_id=conversation, rel_paths=['notes.txt'])
        settled = flask_client.post(
            f'/api/v4/conversations/{conversation}/turns/settled', json={
                'commandId': f'settle-{task}', 'actor': 'assistant', 'kind': 'fixture',
                'projection': {'content': 'Edited notes.txt'},
            })
        assert settled.status_code == 200, settled.get_data(as_text=True)
        turn_id = settled.get_json()['turn']['turnId']
        assert apply_commit_round_file_changes(
            conversation, turn_id, files=[{'path': 'notes.txt', 'action': 'written'}],
            modified_count=1, task_id=task, user_id=1)
        yield {'conversation': conversation, 'turn': turn_id, 'root': root}
    finally:
        delete_conversation(conversation)
        with pm_config._lock:
            pm_config._roots.clear()
            pm_config._state['path'] = ''


def _undo(flask_client, file_turn):
    from lib.turn_lifecycle import get_turn

    current = get_turn(file_turn['conversation'], file_turn['turn'], user_id=1)
    return flask_client.post(
        f"/api/v4/conversations/{file_turn['conversation']}/turns/"
        f"{file_turn['turn']}/file-changes/undo",
        json={'commandId': f'undo-{uuid.uuid4().hex[:12]}',
              'expectedProjectionRevision': current['projectionRevision']})


def _file_changes(file_turn):
    from lib.turn_lifecycle import get_turn

    turn = get_turn(file_turn['conversation'], file_turn['turn'], user_id=1)
    return turn['projection']['fileChanges']


def test_refusal_answers_400_with_its_authored_message(flask_client, file_turn):
    notes = file_turn['root'] / 'notes.txt'
    notes.write_text('user edit after the turn\n')

    response = _undo(flask_client, file_turn)

    message = 'Could not undo notes.txt: File changed after the recorded modification'
    assert response.status_code == 400
    assert response.get_json()['error'] == message
    block = _file_changes(file_turn)
    assert block['state'] == 'applied'
    assert block['error'] == message
    assert notes.read_text() == 'user edit after the turn\n'


def _deny_restore(monkeypatch, file_turn):
    from lib.project_mod import undo_filesystem

    withheld = '/srv/private/project/notes.txt'

    def deny(*_args, **_kwargs):
        raise PermissionError(13, 'Permission denied', withheld)

    monkeypatch.setattr(undo_filesystem, 'write_file', deny)
    return ([withheld, 'Permission denied'],
            'Could not undo notes.txt: internal error (PermissionError)', None)


def _tear_journal(monkeypatch, file_turn):
    from lib.project_mod import modifications as mods

    index = Path(mods._get_session_dir(str(file_turn['root']))) / 'modifications.json'
    committed = index.read_bytes()
    index.write_text('{"modifications": [')
    return (['Expecting', 'line 1 column'],
            "Could not undo the turn's file changes: internal error (JSONDecodeError)",
            lambda: index.write_bytes(committed))


@pytest.mark.parametrize('inject', [_deny_restore, _tear_journal],
                         ids=['per-file-restore', 'journal-read'])
def test_engine_fault_answers_a_logged_redacted_500(
        flask_client, file_turn, monkeypatch, caplog, inject):
    withheld, recorded, restore = inject(monkeypatch, file_turn)

    try:
        with caplog.at_level(logging.ERROR):
            response = _undo(flask_client, file_turn)
    finally:
        if restore is not None:
            restore()

    assert response.status_code == 500
    text = response.get_data(as_text=True)
    assert all(fragment not in text for fragment in withheld)
    assert response.get_json()['error']['detail'] == 'FileHistoryFault'
    block = _file_changes(file_turn)
    assert block['state'] == 'applied'
    assert block['error'] == recorded
    assert withheld[0] in caplog.text
    assert (file_turn['root'] / 'notes.txt').read_text() == 'assistant edit\n'
