"""Worktree file-change facts render after settlement without unsafe undo.

Real Chromium and the production view-model/renderers consume a projection
built by the backend authority. This component test does not write user data.
"""
_AUDIT_SYNTHETIC_REPO_PATHS = {'tests/test_tracker.py'}

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (
    frontend_visual_origin, frontend_visual_page,  # noqa: F401
)

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1280])
def test_settled_worktree_file_panel(frontend_visual_page, tmp_path, width):  # noqa: F811
    from lib.turn_lifecycle import _task_projection

    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    task = {
        'id': 'worktree-task', 'content': 'Changes integrated.',
        'modifiedFiles': 2,
        'modifiedFileList': [
            {'path': 'src/tracker.py', 'action': 'patched', 'source': 'local',
             'root': 'task-worktree', 'rootPath': '/removed/task-worktree',
             'changeId': 'a' * 32, 'undoAvailable': False},
            {'path': 'tests/test_tracker.py', 'action': 'created', 'source': 'local',
             'root': 'task-worktree', 'rootPath': '/removed/task-worktree',
             'changeId': 'b' * 32, 'undoAvailable': False},
        ],
    }
    projection = _task_projection(task, {})
    assert projection['fileChanges']['undoAvailable'] is False
    page.add_script_tag(path=native_module_path(
        '.native/worktree-file-changes-feature.js', ROOT / 'frontend/src/conversation/index.ts'))
    page.add_script_tag(path=native_module_path(
        '.native/worktree-file-changes-state.js', ROOT / 'frontend/src/core/turn-state.ts'))
    page.evaluate("""projection => {
      document.body.innerHTML='<main class="chat-inner" id="surface"></main>';
      const turn={turnId:'answer',conversationId:'worktree-panel',actor:'assistant',role:'assistant',
        laneId:'main',ordinal:1,status:'completed',projectionRevision:1,projection};
      window.state={...createTurnState('worktree-panel'),turnsById:{answer:turn},laneOrder:{main:['answer']}};
      window.surface=createConversationSurface(document.getElementById('surface'),
        createClassicConversationRenderers({renderSafeMarkdownHtml: text => text}));
      surface.render(selectConversationViewModel(state));
    }""", projection)
    panel = page.locator('.conversation-file-changes')
    assert panel.is_visible()
    assert panel.locator('.fc-file').count() == 2
    assert '2 files changed' in panel.locator('.fc-summary-text').inner_text()
    assert 'task-worktree:src/tracker.py' in panel.inner_text()
    assert panel.locator('[data-conversation-action="undo-turn-files"]').count() == 0
    assert panel.locator('[data-conversation-action="redo-turn-files"]').count() == 0
    panel.locator('summary').click()
    assert not panel.locator('.fc-details').is_visible()
    page.evaluate("""() => {
      state.turnsById.answer.projectionRevision++;
      surface.render(selectConversationViewModel(state));
    }""")
    assert not panel.locator('.fc-details').is_visible()
    panel.locator('summary').click()
    assert panel.locator('.fc-details').is_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')
    page.screenshot(path=str(tmp_path / f'worktree-file-panel-{width}.png'))
    page.evaluate('surface.dispose()')
