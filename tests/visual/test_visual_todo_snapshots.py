"""Checklist disclosure/layout in Chromium on an isolated locally served app."""
import json

import pytest

from tests._runtime_sections import ROOT, runtime_section

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('viewport_width', [1100, 390])

@pytest.mark.parametrize('language', ['en', 'zh'])
def test_flat_checklist_snapshots_keep_progress_and_audit_verdicts(
    frontend_visual_page, tmp_path, viewport_width, language,
):
    page = frontend_visual_page
    page.set_viewport_size({'width': viewport_width, 'height': 900})
    # The live app fixture loads shipped CSS. Keep this renderer fixture isolated
    # from its runtime: no production globals or persisted conversations changed.
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.evaluate("""() => {
      const frame = document.createElement('iframe');
      frame.id = 'todo-snapshot-fixture';
      frame.style.cssText = 'position:fixed;inset:16px;width:min(700px,calc(100vw - 32px));height:800px;z-index:99999';
      document.body.append(frame);
    }""")
    frame = page.frames[-1]
    frame.set_content('<!doctype html><html data-theme="light"><body><main id="cards"></main></body></html>')
    frame.add_style_tag(path=str(ROOT / 'static/styles.css'))
    frame.evaluate(r"""locale => {
      window.t = (key, params) => (locale[key] || key).replace(/\{(\w+)\}/g,
        (match, name) => params && params[name] !== undefined ? params[name] : match);
      window.escapeHtml = s => String(s).replaceAll('&','&amp;').replaceAll('<','&lt;')
        .replaceAll('>','&gt;').replaceAll('"','&quot;');
      window._rowRightControls = () => '';
    }""", locale)
    frame.add_script_tag(content=runtime_section('ui/tool_rounds_rich.js'))
    frame.evaluate("""() => {
      const snapshots = [
        {todos:[{content:'Original work',status:'in_progress'}],todoUpdateCount:1},
        {todos:[{content:'Replacement work',status:'completed'}],todoUpdateCount:2},
        {todos:[{content:'New pending work',status:'pending'}],todoUpdateCount:3,
          todoExplanation:'Next <script>phase</script>\\nVerify this longer explanation wraps on narrow screens: ' + 'unbroken'.repeat(20)},
        {todos:[{content:'Historical work',status:'blocked'}],todoUpdateCount:3,
          todoRejected:true,todoRejectReason:'Historical sync rejection must remain visible'},
        {todos:[],todoUpdateCount:3,todoNoop:true},
      ];
      document.querySelector('#cards').innerHTML = snapshots.map((meta, n) =>
        _renderTodoBlock({roundNum:n+1,results:[meta]},'', '', '')).join('');
    }""")
    cards = frame.locator('.ptool-todo-block')
    assert cards.count() == 5
    assert not cards.nth(2).locator('.ptool-todo-text').is_visible()

    explanation = cards.nth(2).locator('.ptool-todo-body .ptool-todo-explanation')
    assert not explanation.is_visible()
    assert cards.nth(2).locator('summary .ptool-todo-explanation').count() == 0
    assert explanation.text_content().startswith('Next <script>phase</script>\nVerify')
    label = cards.nth(2).locator('summary .ptool-text')
    assert label.evaluate('(el) => el.getBoundingClientRect().height < 40')
    assert label.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    assert label.evaluate('(el) => el.getBoundingClientRect().width > el.getBoundingClientRect().height')
    assert cards.nth(2).locator('.ptool-todo-explanation').count() == 1
    assert frame.locator('body').evaluate('(el) => el.scrollWidth <= window.innerWidth')
    collapsed_screenshot = tmp_path / f'todo-explanation-collapsed-{viewport_width}.png'
    page.locator('#todo-snapshot-fixture').screenshot(path=str(collapsed_screenshot))
    print(f'Collapsed checklist screenshot: {collapsed_screenshot}')
    cards.nth(2).locator('summary').click()
    assert cards.nth(2).locator('.ptool-todo-text').inner_text() == 'New pending work'
    assert cards.nth(2).locator('[role="progressbar"]').get_attribute('aria-valuenow') == '0'
    assert cards.nth(1).locator('[role="progressbar"]').get_attribute('aria-valuenow') == '100'
    assert explanation.is_visible()
    assert explanation.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    assert cards.nth(2).locator('.ptool-todo-explanation').count() == 1
    assert cards.nth(2).locator('script, .ptool-todo-breadcrumbs').count() == 0
    cards.nth(2).locator('summary').click()
    assert not cards.nth(2).locator('.ptool-todo-text').is_visible()
    assert not explanation.is_visible()
    assert cards.nth(3).locator('summary').inner_text().startswith(locale['todo.rejected'])
    assert cards.nth(4).locator('summary').inner_text().startswith(locale['todo.unchanged'])
    cards.nth(3).locator('summary').click()
    assert cards.nth(3).locator('.ptool-todo-rejection-detail').is_visible()
    screenshot = tmp_path / 'todo-flat-snapshots.png'
    page.locator('#todo-snapshot-fixture').screenshot(path=str(screenshot))
    print(f'Checklist screenshot: {screenshot}')

    # Smoke the published runtime path too, not only the isolated rich renderer.
    frame.evaluate("window.Icon=()=>''; window.renderMarkdown=escapeHtml; window._shortUrl=String; window.formatNumber=String;")
    frame.add_script_tag(content=runtime_section('ui/tool_rounds.js'))
    markup = frame.evaluate("""() => _renderUnifiedToolLine({
      toolName: 'todo_write', toolCallId: 'shipped-todo-explanation',
      roundNum: 1, status: 'done', results: [{
        todos: [{content: 'Shipped runtime item', status: 'pending'}],
        todoExplanation: 'The shipped renderer also exposes the plan rationale.',
      }],
    }, false)""")
    frame.evaluate("markup => document.querySelector('#cards').innerHTML = markup", markup)
    # Open outer tool grouping only; the checklist itself remains collapsed.
    frame.evaluate("""() => document.querySelectorAll('.collapsed').forEach(el => {
      if (!el.matches('.ptool-todo-block')) el.classList.remove('collapsed');
    })""")
    shipped_card = frame.locator('.ptool-todo-block')
    assert shipped_card.count() == 1
    assert not shipped_card.evaluate('(el) => el.open')
    assert shipped_card.locator('summary .ptool-todo-explanation').count() == 0
    shipped_card.locator('summary').click()
    assert shipped_card.locator('.ptool-todo-explanation').is_visible()
    assert shipped_card.locator('.ptool-todo-text').is_visible()
