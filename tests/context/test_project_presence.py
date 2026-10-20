"""Tests for the project_presence context block."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lib.tasks_pkg.context_composer._models import ComposeRequest
from lib.tasks_pkg.context_composer._providers import _project_presence_block

pytestmark = pytest.mark.unit


def _request(task=None, conv_id='', user_id=1, project_path='/test/project'):
    return ComposeRequest(
        project_path=project_path,
        project_enabled=True,
        conv_id=conv_id,
        user_id=user_id,
        task=task or {},
    )


def _mock_binding(*, available=True, conversation_id='coord-1'):
    return {
        'projectPath': '/test/project',
        'availability': 'available' if available else 'unavailable',
        'conversationId': conversation_id,
    }


def _mock_task(conv_id='worker-1', status='running', updated_at=None, events=None, title='Test task', project_path='/test/project'):
    import time
    return {
        'convId': conv_id,
        'status': status,
        'updated_at': updated_at if updated_at is not None else time.time(),
        'events': events or [],
        'config': {'projectPath': project_path},
        'meta': {'title': title},
    }


class TestProjectPresenceBlock:

    def test_empty_when_not_coordinator(self):
        req = _request(conv_id='not-coordinator')
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls:
            repo_cls.return_value.read.return_value = _mock_binding(conversation_id='coord-1')
            assert _project_presence_block(req) == ''

    def test_empty_when_binding_unavailable(self):
        req = _request(conv_id='coord-1')
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls:
            repo_cls.return_value.read.return_value = _mock_binding(available=False)
            assert _project_presence_block(req) == ''

    def test_empty_when_no_peers(self):
        req = _request(conv_id='coord-1')
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = []
            assert _project_presence_block(req) == ''

    def test_renders_running_worker(self):
        import time
        req = _request(conv_id='coord-1')
        peer = _mock_task(
            conv_id='worker-a1b2',
            status='running',
            updated_at=time.time() - 60,
            events=[{'type': 'tool_call', 'name': 'read_files'}],
            title='Refactor auth module',
        )
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer]
            result = _project_presence_block(req)
        assert '[Project Presence]' in result
        assert '●' in result
        assert '"Refactor auth module"' in result
        assert 'worker-a1b2' in result
        assert 'running' in result
        assert 'read_files' in result

    def test_renders_stalled_worker(self):
        import time
        req = _request(conv_id='coord-1')
        peer = _mock_task(
            conv_id='worker-c3d4',
            status='running',
            updated_at=time.time() - 300,
            title='Fix pagination bug',
        )
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer]
            result = _project_presence_block(req)
        assert '◐' in result
        assert 'stalled' in result

    def test_renders_terminal_worker(self):
        import time
        req = _request(conv_id='coord-1')
        peer = _mock_task(
            conv_id='worker-e5f6',
            status='error',
            updated_at=time.time() - 60,
            title='Add unit tests',
        )
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer]
            result = _project_presence_block(req)
        assert '○' in result
        assert 'error' in result

    def test_filters_by_project_path(self):
        req = _request(conv_id='coord-1')
        peer_same = _mock_task(conv_id='worker-1', title='Same project')
        peer_other = _mock_task(conv_id='worker-2', title='Other project', project_path='/other/project')
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer_same, peer_other]
            result = _project_presence_block(req)
        assert 'worker-1' in result
        assert 'worker-2' not in result
        assert 'Other project' not in result

    def test_skips_unknown_status(self):
        req = _request(conv_id='coord-1')
        peer = _mock_task(status='unknown_state', title='Weird')
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer]
            result = _project_presence_block(req)
        assert result == ''

    def test_last_event_text_message(self):
        import time
        req = _request(conv_id='coord-1')
        peer = _mock_task(
            conv_id='worker-x',
            status='running',
            updated_at=time.time(),
            events=[{'type': 'text', 'text': 'Analyzing the auth flow and planning changes'}],
        )
        with patch('lib.conversations.project_coordination.ProjectCoordinationRepository') as repo_cls, \
             patch('lib.tasks_pkg.manager.runtime.chat_task_runtime') as runtime:
            repo_cls.return_value.read.return_value = _mock_binding()
            runtime.snapshot_owned.return_value = [peer]
            result = _project_presence_block(req)
        assert 'last: "Analyzing the auth flow' in result
