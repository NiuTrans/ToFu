"""Fail-closed contract for Project Brain isolated epic creation."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_isolation_creation_failure_blocks_instead_of_shared_dispatch(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lib.conversations import project_board as board
    from lib.conversations import project_dispatch as dispatch
    import lib.integration_control as integration

    commands: list[tuple[str, dict]] = []

    class _Storage:
        def command(self, operation, payload, command_id):
            del command_id
            commands.append((operation, dict(payload)))
            if operation == 'board.post':
                return {'ok': True, 'id': 'pt_isolated', 'title': payload['title']}
            if operation == 'board.mutate' and payload.get('action') == 'block':
                return {
                    'ok': True, 'title': 'isolated epic', 'block_count': 1,
                    'blocked_until': 0,
                }
            raise AssertionError((operation, payload))

    dispatched: list[str] = []
    monkeypatch.setattr(board, 'get_storage_client', lambda **_kw: _Storage())
    monkeypatch.setattr(board, '_emit', lambda *_a, **_kw: None)
    monkeypatch.setattr(board, 'audit_log', lambda *_a, **_kw: None)
    monkeypatch.setattr(
        integration, 'create_workspace',
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError('git unavailable')),
    )
    monkeypatch.setattr(
        dispatch, 'on_epic_posted',
        lambda _path, task_id, **_kw: dispatched.append(task_id),
    )

    result = board.post_task(
        str(tmp_path), 'conv-a', 'isolated epic', user_id=1,
        isolated=True, write_set=['lib/'],
    )

    assert result['ok'] is True
    assert result['isolated'] is False
    assert result['isolationBlocked'] is True
    assert 'git unavailable' in result['isolationError']
    assert dispatched == []
    assert [name for name, _payload in commands] == ['board.post', 'board.mutate']
    block_payload = commands[-1][1]
    assert block_payload['action'] == 'block'
    assert 'isolated workspace unavailable' in block_payload['reason']
