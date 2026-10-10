"""Terminal content remains visible after an older checkpoint ending at Poll."""
import pytest
from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

@pytest.mark.parametrize('width', [1180, 390])
def test_saved_final_answer_after_poll_survives_replay(frontend_visual_page, width):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("document.body.innerHTML = '<main id=surface></main>'")
    page.add_script_tag(path=native_module_path('.native/missing-final-feature.js',
        ROOT / 'frontend/src/conversation/index.ts'))
    page.add_script_tag(path=native_module_path('.native/missing-final-state.js',
        ROOT / 'frontend/src/core/turn-state.ts'))
    page.evaluate("""() => {
      const turn = {turnId:'turn',conversationId:'missing-final',actor:'assistant',
        laneId:'main',ordinal:1,status:'completed',projectionRevision:1,
        settlement:{outcome:'completed'},projection:{content:'Saved final answer after Poll.',
          segments:[{type:'tool_use',blockId:'poll',id:'poll',name:'poll',input:{}}]}};
      window.state = {...createTurnState('missing-final'),turnsById:{turn},laneOrder:{main:['turn']}};
      window.surface = createConversationSurface(document.getElementById('surface'), {});
      window.render = () => surface.render(selectConversationViewModel(state));
      render();
    }""")
    answer = page.locator('[data-block-id="text:terminal"]')
    assert answer.is_visible()
    assert 'Saved final answer after Poll.' in answer.inner_text()
    assert answer.count() == 1
    assert page.locator('[data-block-id="poll"]').is_visible()
    page.evaluate('state = JSON.parse(JSON.stringify(state)); render()')
    assert answer.count() == 1
    assert answer.is_visible()
    poll_box = page.locator('[data-block-id="poll"]').bounding_box()
    answer_box = answer.bounding_box()
    assert answer_box['y'] >= poll_box['y'] + poll_box['height'] - 1
