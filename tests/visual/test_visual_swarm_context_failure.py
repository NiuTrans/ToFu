"""Real panel rendering distinguishes local admission failure on live/reload cards."""
import json

import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual
FAILURE = ('Automatic context compaction failed locally before provider dispatch '
           '(irreducible_prompt_floor); the prompt remains 115,604 tokens. '
           'No main-provider request was sent.')


@pytest.mark.parametrize('width', [390, 1100])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_local_context_failure_cards(frontend_visual_page, tmp_path, width, language):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 950})
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate(r"""locale => {
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => params?.[name] ?? match);
      window.escapeHtml = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window.runtimeScope = {}; window._TOOL_DISPLAY = {};
      window.errorEnvelopeMessage = String;
      document.body.innerHTML = '<main id="chat"></main>';
    }""", locale)
    page.add_script_tag(path=native_module_path(
        '.native/swarm-context-failure.js', ROOT / 'frontend/src/conversation/presentation/swarm-failure-presentation.ts'))
    page.add_script_tag(content=runtime_section('ui/streaming_swarm_panel.js'))
    page.evaluate("""failure => {
      window.receipt = {roundNum:1,_swarm:true,_swarmActive:false,status:'done',
        _swarmAgents:['formal','selection','deployment','cc-friday'].map(id => ({
          id:'agent-general-'+id,role:'general',objective:'Inspect assigned evidence',status:'failed',
          error:failure,elapsed:5.1,tokens:0}))};
      window.repaint = () => { document.getElementById('chat').innerHTML = _buildSwarmPanelHTML(receipt); };
      repaint();
    }""", FAILURE)
    for _ in range(2):
        assert page.locator('.sw-agent').count() == 4
        assert page.locator('.sw-a-err-title').all_text_contents() == [locale['swarm.failure.contextPreparation']] * 4
        assert locale['swarm.failure.contextPreparationRecovery'] in page.locator('.sw-a-err').first.inner_text()
        assert locale['swarm.failure.genericRecovery'] not in page.locator('#chat').inner_text()
        details = page.locator('.sw-a-err-details').first
        assert not details.locator('.sw-a-err-reason').is_visible()
        details.locator('summary').click()
        assert details.locator('.sw-a-err-reason').inner_text() == FAILURE
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(tmp_path / f'swarm-context-{language}-{width}.png'), full_page=True)
        page.evaluate('repaint()')
