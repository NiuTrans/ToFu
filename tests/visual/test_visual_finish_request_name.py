"""Real Chromium: the Finish Tag shows the complete served request name.

The footer must never clip the request name to a prefix (the 220px cap cut
``kunlunbedrock.claude-opus-5.5-m17`` to ``…-5.5-m``). A name wider than the
whole footer row wraps inside the row instead of overflowing it.
"""
import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

REQUEST_NAME = 'kunlunbedrock.claude-opus-5.5-m17'
ROW_WIDER_REQUEST_NAME = (
    'kunlunbedrock.us.anthropic.claude-opus-5.5-m17-20261023-'
    'extended-thinking-long-context-preview-v1:0')


@pytest.mark.parametrize('theme', ['dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1280])
def test_finish_tag_shows_complete_request_name(frontend_visual_page, theme, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 700})
    page.add_script_tag(path=native_module_path(
        '.native/finish-request-name-visual.js',
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
    rows = page.evaluate("""([theme, names]) => {
      document.documentElement.setAttribute('data-theme', theme);
      const message = name => ({role:'assistant', content:'Answer', finishReason:'stop',
        _turnStatus:'completed', model:'claude-opus-5.5', thinkingDepth:'medium', toolRounds:[],
        lastRoundUsage:{resolvedModel:name, providerId:'sankuai', keyTail:'4861',
          reasoningResolution:{requested:'preset', requestedValue:'medium',
            actualMode:'exact', actualValue:'max'}}});
      // Same unwrap as ConversationTurnStore.renderTurnFooterHtml.
      const footerInner = html => {
        const holder = document.createElement('div');
        holder.innerHTML = html;
        return holder.firstElementChild.innerHTML;
      };
      document.getElementById('preview').innerHTML = names.map((name, index) =>
        '<div class="conversation-surface"><article class="conversation-turn message">'
        + '<div id="footer' + index + '" class="conversation-turn-footer message-finish">'
        + footerInner(renderFinishInfo(message(name))) + '</div></article></div>').join('');
      return names.map((name, index) => {
        const footer = document.getElementById('footer' + index);
        const tag = footer.querySelector('.finish-tag.preset');
        const range = document.createRange();
        range.selectNodeContents(tag);
        const box = tag.getBoundingClientRect();
        const text = range.getBoundingClientRect();
        const lineTops = [...range.getClientRects()]
          .filter(rect => rect.width > 0).map(rect => Math.round(rect.top));
        return {
          name, label: tag.textContent.trim(),
          tag: {left: box.left, right: box.right, top: box.top, bottom: box.bottom},
          text: {left: text.left, right: text.right, top: text.top, bottom: text.bottom},
          rowRight: footer.getBoundingClientRect().right,
          scrollWidth: tag.scrollWidth, clientWidth: tag.clientWidth,
          lines: new Set(lineTops).size,
        };
      });
    }""", [theme, [REQUEST_NAME, ROW_WIDER_REQUEST_NAME]])

    for row in rows:
        assert row['label'] == f"{row['name']} Medium → Max"
        # Laid-out glyphs must sit inside the painted tag box: a clipped tag
        # still reports full textContent, but its text range overruns the box.
        assert row['text']['left'] >= row['tag']['left'] - 1, row
        assert row['text']['right'] <= row['tag']['right'] + 1, row
        assert row['text']['bottom'] <= row['tag']['bottom'] + 1, row
        assert row['scrollWidth'] <= row['clientWidth'] + 1, row
        assert row['tag']['right'] <= row['rowRight'] + 1, row
    if width >= 1280:
        assert rows[0]['lines'] == 1, rows[0]
    else:
        assert rows[1]['lines'] > 1, rows[1]
    page.screenshot(path=str(tmp_path / f'finish-request-name-{theme}-{width}.png'))
