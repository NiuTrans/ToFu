"""Real Chromium: an attributed subscription-quota fact stays inside the footer.

A mixed turn names the calls behind its quota reading in the first quota fact
(``订阅（内部压缩 gpt-6-luna） · 7 天余99%``).  Quota facts are separate
flex items, so the row wraps between them; a source list wider than a narrow
footer row must still wrap inside its own tag rather than overflow the row.
"""
import json

import pytest

from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

ZH_COPY = json.loads(
    (ROOT / 'frontend/src/i18n/locales/zh.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('theme', ['dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1280])
def test_attributed_quota_facts_stay_inside_footer(frontend_visual_page, theme, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 700})
    page.add_script_tag(path=native_module_path(
        '.native/token-usage.js', ROOT / 'frontend/src/token-usage.ts'))
    page.add_script_tag(path=native_module_path(
        '.native/finish-request-name-visual.js',
        ROOT / 'frontend/src/conversation/presentation/turn-serving-route.ts'))
    page.add_script_tag(content='window.__zhCopy = ' + json.dumps(ZH_COPY, ensure_ascii=False) + ';')
    page.add_script_tag(content=r"""
      window.escapeHtml = value => String(value ?? '').replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.t = (key, params) => {
        let value = Object.hasOwn(window.__zhCopy, key) ? window.__zhCopy[key] : key;
        for (const [name, replacement] of Object.entries(params || {}))
          value = value.replaceAll('{' + name + '}', String(replacement));
        return value;
      };
      window.Icon = () => '';
      window.calcCostCny = window.calcCost = () => null;
      window.formatCny = () => '0';
      window._isThinkingCapable = () => true;
      document.body.innerHTML = '<main id="preview"></main>';
    """)
    page.add_script_tag(path=runtime_section_path('ui/finish_info.js'))
    page.add_style_tag(content='body{display:block;overflow:auto}main{margin:24px;max-width:900px}')
    rows = page.evaluate("""(theme) => {
      document.documentElement.setAttribute('data-theme', theme);
      const quota = () => ({provider:'codex', source:'response_headers',
        primary:{window_minutes:10080, used_percent:1, remaining_percent:99,
                 has_previous_snapshot:true, observed_delta_percent:0},
        secondary:{used_percent:0, remaining_percent:100,
                   has_previous_snapshot:true, observed_delta_percent:0}});
      const metered = round => ({round, model:'claude-opus-5.5',
        usage:{prompt_tokens:166000, completion_tokens:2300,
               _dispatch:{model:'kunlunbedrock.claude-opus-5.5-m17', provider_id:'example-corp'}}});
      const subscription = (round, model, kind) => ({round, kind, model,
        provider_id:'oauth_codex',
        usage:{prompt_tokens:16018, completion_tokens:1151,
               _dispatch:{model, provider_id:'oauth_codex'}, _subscription_quota:quota()}});
      const message = apiRounds => ({role:'assistant', content:'Answer',
        finishReason:'stop', _turnStatus:'completed', model:'claude-opus-5.5',
        thinkingDepth:'max', toolRounds:[], apiRounds,
        usage:{prompt_tokens:6316908, completion_tokens:89470},
        lastRoundUsage:{resolvedModel:'kunlunbedrock.claude-opus-5.5-m17',
          providerId:'example-corp', keyTail:'4861'}});
      const turns = [
        message([metered(1), metered(2), subscription('compaction-1', 'gpt-6-luna', 'compaction')]),
        message([subscription(1, 'gpt-6.1-sol-extended-context-preview'),
                 subscription('compaction-1', 'gpt-6-luna-extended-context-preview', 'compaction'),
                 metered(2)]),
      ];
      // Same unwrap as ConversationTurnStore.renderTurnFooterHtml.
      const footerInner = html => {
        const holder = document.createElement('div');
        holder.innerHTML = html;
        return holder.firstElementChild.innerHTML;
      };
      document.getElementById('preview').innerHTML = turns.map((turn, index) =>
        '<div class="conversation-surface"><article class="conversation-turn message">'
        + '<div id="footer' + index + '" class="conversation-turn-footer message-finish">'
        + footerInner(renderFinishInfo(turn)) + '</div></article></div>').join('');
      return turns.map((turn, index) => {
        const footer = document.getElementById('footer' + index);
        const rowRight = footer.getBoundingClientRect().right;
        return [...footer.querySelectorAll('.subscription-quota-tag')].map(tag => {
          const range = document.createRange();
          range.selectNodeContents(tag);
          const box = tag.getBoundingClientRect();
          const text = range.getBoundingClientRect();
          return {
            label: tag.textContent.trim(), rowRight,
            tag: {left: box.left, right: box.right, bottom: box.bottom},
            text: {left: text.left, right: text.right, bottom: text.bottom},
            scrollWidth: tag.scrollWidth, clientWidth: tag.clientWidth,
            lines: new Set([...range.getClientRects()]
              .filter(rect => rect.width > 0).map(rect => Math.round(rect.top))).size,
          };
        });
      });
    }""", theme)
    page.screenshot(path=str(tmp_path / f'finish-quota-attribution-{theme}-{width}.png'))

    single, multiple = rows
    assert single[0]['label'] == '订阅（内部压缩 gpt-6-luna） · 7 天余99%'
    assert single[2]['label'] == '订阅调用 17.2k tok'
    assert multiple[0]['label'].startswith(
        '订阅（gpt-6.1-sol-extended-context-preview、内部压缩 gpt-6-luna-extended-context-preview）')
    for tag in single + multiple:
        assert tag['text']['left'] >= tag['tag']['left'] - 1, tag
        assert tag['text']['right'] <= tag['tag']['right'] + 1, tag
        assert tag['text']['bottom'] <= tag['tag']['bottom'] + 1, tag
        assert tag['scrollWidth'] <= tag['clientWidth'] + 1, tag
        assert tag['tag']['right'] <= tag['rowRight'] + 1, tag
    assert single[0]['lines'] == 1, single[0]
    if width >= 1280:
        assert multiple[0]['lines'] == 1, multiple[0]
    else:
        assert multiple[0]['lines'] > 1, multiple[0]
