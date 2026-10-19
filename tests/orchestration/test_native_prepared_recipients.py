"""Prepared occurrence dispatch and context ownership also use the native cursor."""
import pytest

from lib.identity import PrincipalContext
from lib.orchestration.engine import FlowExecutor
from tests.orchestration import test_orchestration_prepared_recipients as oracle
from tests.orchestration.test_native_flow_engine import native_library as native_library

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('shape,conversation', [
    ('linear', False), ('linear', True), ('parallel', False), ('isolated', True),
    ('fanout', False), ('nested_parallel', False)])
def test_prepared_recipients_preserve_native_flow_cursor_and_context(shape, conversation, native_library, monkeypatch):
    original = FlowExecutor.__init__
    engines = []
    principal = PrincipalContext.user(subject_id='prepared-flow', owner_user_id=71)
    def initialize(self, definition, **options):
        parent = options.get('parent_task')
        if parent is None:
            parent = dict(id='prepared-flow-source', _userId=71)
            options['parent_task'] = parent
        parent['_principalContext'] = principal.to_payload()
        options['native_flow_library'] = native_library
        original(self, definition, **options)
        engines.append(self)
    monkeypatch.setattr(FlowExecutor, '__init__', initialize)
    monkeypatch.setattr(FlowExecutor, '_walk_python', lambda *args, **kwargs: pytest.fail('native cursor fell back'))
    oracle.test_prepared_recipients_complete_through_actual_graph_context_and_journal(
        monkeypatch, shape, conversation)
    assert engines and all(engine._native_flow_library is native_library for engine in engines)
    assert not native_library._graphs
