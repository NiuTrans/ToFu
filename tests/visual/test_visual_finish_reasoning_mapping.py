"""Real Chromium: retained Finish Tag renders authoring receipt, never metadata."""
import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1280])
def test_finish_reasoning_receipt(frontend_visual_page, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 700})
    page.add_script_tag(path=native_module_path(
        '.native/finish-reasoning-visual.js',
        ROOT / 'frontend/src/conversation/presentation/turn-serving-route.ts'))
    page.add_script_tag(content=r"""
      window.escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.t = key => key;
      window.Icon = () => '';
      window.calcCostCny = window.calcCost = () => 0;
      window.formatCny = () => '0';
      window._isThinkingCapable = () => true;
      document.body.innerHTML = '<main id="preview"></main>';
    """)
    page.add_script_tag(path=runtime_section_path('ui/finish_info.js'))
    page.add_style_tag(content='body{display:block;overflow:auto}main{margin:24px;max-width:900px}')
    page.evaluate("""() => {
      const resolution = actual => ({requested:'preset',requestedValue:'medium',
        actualMode:'exact',actualValue:actual});
      const base = {role:'assistant',content:'Answer',finishReason:'stop',
        _turnStatus:'completed',model:'kimi-k3',thinkingDepth:'medium',toolRounds:[]};
      const cases = [
        {...base,lastRoundUsage:{resolvedModel:'kimi-k3',reasoningResolution:resolution('max')}},
        {...base,fallbackModel:'fallback-model',fallbackFrom:'kimi-k3',
          apiRounds:[{kind:'agent',usage:{_reasoning_resolution:resolution('high')}},
            {kind:'compaction',usage:{_reasoning_resolution:resolution('max')}},
            {kind:'agent',tag:'retry-discarded',usage:{_reasoning_resolution:resolution('max')}}]},
        {...base,lastRoundUsage:{reasoningResolution:resolution('medium')}},
        {...base,thinkingDepth:'off',lastRoundUsage:{reasoningResolution:
          {requested:'off',actualMode:'off',actualValue:''}}},
        {...base,thinkingDepth:'default',lastRoundUsage:{reasoningResolution:
          {requested:'default',actualMode:'default',actualValue:''}}},
        base,
        {...base,lastRoundUsage:{reasoningResolution:resolution('<img src=x onerror="window.pwned=1">')}}
      ];
      document.getElementById('preview').innerHTML = cases.map((msg,i) =>
        '<section id="case'+i+'">'+renderFinishInfo(msg)+'</section>').join('');
    }""")
    for index, text in enumerate(['Medium → Max', 'Medium → High', 'Medium', 'Off',
                                  'Default (requested)', 'Medium (requested)']):
        label = page.locator(f'#case{index} .finish-tag.preset')
        assert label.is_visible()
        assert text in label.inner_text()
    assert '→' not in page.locator('#case2').inner_text()
    assert '→' not in page.locator('#case5').inner_text()
    assert page.locator('#preview img').count() == 0
    assert page.evaluate('window.pwned') is None
    page.screenshot(path=str(tmp_path / f'finish-reasoning-{width}.png'))
