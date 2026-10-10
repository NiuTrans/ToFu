"""Conversation-reference reads require and preserve explicit ownership."""

import pytest

pytestmark = pytest.mark.unit


class TestQuerySignature:
    """The functions must ACCEPT an explicit user_id (the wire must exist)."""

    def test_list_conversations_accepts_user_id(self):
        import inspect
        from lib.conv_ref._query import list_conversations
        assert 'user_id' in inspect.signature(list_conversations).parameters

    def test_get_conversation_accepts_user_id(self):
        import inspect
        from lib.conv_ref._detail import get_conversation
        assert 'user_id' in inspect.signature(get_conversation).parameters

    def test_execute_conv_ref_tool_accepts_user_id(self):
        import inspect
        from lib.conv_ref._tool import execute_conv_ref_tool
        assert 'user_id' in inspect.signature(execute_conv_ref_tool).parameters

    def test_build_digest_accepts_user_id(self):
        """The human-facing digest reads the same table and needs the same scoping."""
        import inspect
        from lib.conv_ref._detail import build_conversation_digest
        assert 'user_id' in inspect.signature(build_conversation_digest).parameters


class TestUserIdReachesTheAuthority:
    """An explicit owner must reach the repository on every read."""

    def test_list_conversations_passes_the_given_user_id(self, monkeypatch):
        from lib.conv_ref import _query
        import lib.conversations.repository as repository
        seen = {}

        def _list(**kwargs):
            seen.update(kwargs)
            return []

        monkeypatch.setattr(repository, 'list_conversations', _list)
        _query.list_conversations(scope='all', user_id=7)
        assert seen['user_id'] == 7

    def test_list_conversations_rejects_missing_owner(self, monkeypatch):
        from lib.conv_ref import _query
        import lib.conversations.repository as repository

        def _list(**_kwargs):
            pytest.fail('missing owner reached the repository')

        monkeypatch.setattr(repository, 'list_conversations', _list)
        with pytest.raises(TypeError):
            _query.list_conversations(scope='all')

    def test_get_conversation_passes_the_given_user_id(self, monkeypatch):
        from lib.conv_ref import _detail
        seen = {}

        def _read(conversation_id, *, user_id, **projection):
            del projection
            seen['conversation_id'] = conversation_id
            seen['user_id'] = user_id
            return None

        monkeypatch.setattr(_detail, '_read_conversation_snapshot', _read)
        _detail.get_conversation('someconv', user_id=7)
        assert seen == {'conversation_id': 'someconv', 'user_id': 7}

    def test_digest_passes_the_given_user_id(self, monkeypatch):
        from lib.conv_ref import _detail
        seen = {}

        def _read(conversation_id, *, user_id, **projection):
            del conversation_id, projection
            seen['user_id'] = user_id
            return None

        monkeypatch.setattr(_detail, '_read_conversation_snapshot', _read)
        assert _detail.build_conversation_digest('someconv', user_id=9) is None
        assert seen['user_id'] == 9


class TestHandlerThreadsTaskIdentity:
    """The dispatch handler must pass the TASK's owner, not a constant."""

    def test_brain_handler_passes_task_user_id(self):
        """_handle_conv_ref_tool must resolve identity from the task dict.

        Asserted structurally (source-level) because the handler's real call
        path needs the full task/round plumbing; the point being pinned is that
        it does not silently keep calling with no user_id.
        """
        import inspect
        from lib.tasks_pkg.handlers.misc import _brain
        src = inspect.getsource(_brain)
        assert 'task_user_id' in src, (
            'the conv_ref handler does not resolve the task owner — tools will '
            'fall back to user 1 for every tenant'
        )
        assert 'user_id=' in src

    def test_task_user_id_is_the_canonical_helper(self):
        """Pin the helper this wires to, so a future rename fails loudly."""
        from lib.tasks_pkg.manager._registry import task_user_id
        with pytest.raises(ValueError, match='numeric user_id'):
            task_user_id({})
        assert task_user_id({'_userId': 9}) == 9
