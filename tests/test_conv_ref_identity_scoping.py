"""conv_ref multi-tenant identity — user_id must be resolved per call.

``lib/conv_ref/_query.py`` hard-coded ``DEFAULT_USER_ID = 1`` into the
``WHERE user_id=?`` of ``list_conversations``, and ``_detail.get_conversation``
did the same on its single-row SELECT. The comment said "mirrors
routes/common.py", but ``routes/common.py:_request_user_id()`` does NOT
hard-code — it resolves the authenticated principal and only falls back to 1
when there is no login-bound session.

Consequence on a multi-user deployment: both conv_ref tools always read
user 1's conversations regardless of who is asking. For a caller who is not
user 1 that is a cross-tenant read; for user 1's own siblings it is invisible
because the default happens to match.

The fix threads identity the same way pt_abae3a85a92440fd did for
``notify_conv_changed``:
  * request threads → ``routes.common._request_user_id()``
  * background task threads → ``task_user_id(task)`` (reads ``task['_userId']``
    stashed at create_task time)
both landing on an explicit ``user_id=`` parameter with the DEFAULT_USER_ID
fallback preserved byte-identically for the single-user install.
"""

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


class TestUserIdReachesTheSQL:
    """An explicit user_id must actually bind into the query, not be ignored."""

    def _capture(self, monkeypatch, mod):
        """Replace the module's DB accessor with a param-capturing fake."""
        seen = {}

        class _Cur:
            def fetchall(self):
                return []

            def fetchone(self):
                return None

        class _DB:
            def execute(self, sql, params=()):
                seen['sql'] = sql
                seen['params'] = params
                return _Cur()

        monkeypatch.setattr(mod, '_get_db', lambda: _DB())
        return seen

    def test_list_conversations_binds_the_given_user_id(self, monkeypatch):
        from lib.conv_ref import _query
        seen = self._capture(monkeypatch, _query)
        _query.list_conversations(scope='all', user_id=7)
        assert 'user_id=?' in seen['sql']
        assert 7 in tuple(seen['params']), (
            f'user_id=7 never bound; params={seen["params"]}')

    def test_list_conversations_defaults_to_single_user(self, monkeypatch):
        """Omitting user_id must preserve the DEFAULT_USER_ID=1 behaviour."""
        from lib.conv_ref import _query
        seen = self._capture(monkeypatch, _query)
        _query.list_conversations(scope='all')
        assert _query.DEFAULT_USER_ID in tuple(seen['params'])

    def test_get_conversation_binds_the_given_user_id(self, monkeypatch):
        from lib.conv_ref import _detail
        seen = self._capture(monkeypatch, _detail)
        _detail.get_conversation('someconv', user_id=7)
        assert 7 in tuple(seen['params']), (
            f'user_id=7 never bound; params={seen["params"]}')

    def test_get_conversation_scopes_by_user_in_sql(self, monkeypatch):
        """A row belonging to another tenant must not be reachable by id alone."""
        from lib.conv_ref import _detail
        seen = self._capture(monkeypatch, _detail)
        _detail.get_conversation('someconv', user_id=7)
        assert 'user_id' in seen['sql'], (
            'get_conversation SELECT has no user_id predicate — any tenant '
            'could read any conversation by guessing its id')


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
        assert task_user_id({}) == 1
        assert task_user_id({'_userId': 9}) == 9
