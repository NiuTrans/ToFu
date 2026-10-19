"""Navigation must not mutate the shared project registry to paint saved roots."""
import pytest
from tests._runtime_sections import shipped_source_text
from tests.frontend.test_frontend_workspace_attachment_results import _function

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('signature', ['function _restoreConvProject(', 'async function loadProjectStatus(', 'async function clearProject('])
def test_conversation_navigation_and_detach_cannot_write_global_registry(signature):
    body = _function(shipped_source_text('frontend/src/runtime/sections/project_state.js'), signature)
    for mutation in ('Api.project.setPath(', 'Api.project.setPaths(', 'Api.project.clear('):
        assert mutation not in body, 'Conversation projection/detach must stay local to its own selection'


def test_explicit_picker_still_has_a_root_validation_write_boundary():
    body = _function(shipped_source_text('frontend/src/runtime/sections/project.js'), 'async function mpApplyFolders(')
    assert 'Api.project.setPaths(' in body
    assert 'beginProjectMutation' in body
