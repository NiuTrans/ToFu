"""Real-browser edit cards: counted outcomes, row evidence, compact geometry.

Uses the shipped stylesheet and real header/result/renderer owners. The shared
frontend_visual_origin fixture owns an ephemeral loopback server; no application
server, storage, model calls, or external browser services are needed.
"""
import json

import pytest

from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT, frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual

FAILURE_REASON = 'Anchor "expected_old_block" not found in frontend/src/example.ts.'
RECOVERY_HINT = 'Recovery: read the current file and use a unique surrounding anchor.'


def _mount_edit_card(page, *, status, failed, theme):
    for name, source in [
        ('edit-summary-header', 'frontend/src/conversation/presentation/tool-header-presentation.ts'),
        ('edit-summary-result', 'frontend/src/conversation/presentation/tool-result-presentation.ts'),
        ('edit-summary-renderer', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'),
    ]:
        page.add_script_tag(path=native_module_path(f'.native/{name}.js', ROOT / source))
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/en.json').read_text())
    page.evaluate(r'''({locale, status, failed, reason, hint, theme}) => {
      document.documentElement.dataset.theme = theme;
      document.body.innerHTML = '<main id="fixture" class="conversation-surface">'
        + '<div id="tool" class="conversation-block conversation-block--tool"></div></main>';
      const translate = (key, args) => (locale[key] || key)
        .replace(/\{(\w+)\}/g, (_, name) => args?.[name] ?? '');
      const refusal = {resolveRefusal: () => null, renderBadgeHtml: () => '', renderNoticeHtml: () => ''};
      const headers = createToolHeaderPresentation({translate, writeGateRefusal: refusal});
      const results = createToolResultPresentation({translate, writeGateRefusal: refusal});
      const path = 'frontend/src/example.ts';
      const edits = Array.from({length: 6}, (_, index) => ({
        path, operation: 'replace', anchor: `old_${index}`, content: `new_${index}`,
      }));
      const editSummaries = edits.map((edit, index) => ({
        path, operation: edit.operation, description: `Update block ${index + 1}`,
        status: failed && index === 5 ? 'fail' : 'ok',
        detail: failed && index === 5 ? `${path} [replace] [match-not-found]: ${reason}\n${hint}` : '',
      }));
      const lines = editSummaries.map((summary, index) =>
        `[${index + 1}] ${summary.status === 'fail' ? 'FAIL' : 'OK'} ${path} [replace] `
        + (summary.status === 'fail' ? `[match-not-found]: ${reason}\n${hint}` : ': changed'));
      const count = failed ? '5/6' : '6/6';
      const metadata = {type: 'write', writeOk: !failed, badge: `${count} edits`, editSummaries};
      const output = `Applied ${count} edits${failed ? ' (1 failed)' : ''}\n` + lines.join('\n');
      const round = {roundNum: 1, toolName: 'edit_file', status, query: 'Edit frontend/src/example.ts',
        toolArgs: {edits}, results: [metadata], toolContent: output};
      const header = {
        iconHtml: '<svg width="14" height="14" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 20L20 4"/></svg>',
        queryHtml: headers.queryText(round), rootPillHtml: '',
        badgeHtml: headers.renderBadgeHtml(round, metadata),
      };
      const renderers = createClassicConversationRenderers({
        localizedText: (key, fallback) => locale[key] || fallback,
        renderToolBlockHtml: block => results.renderWriteResultHtml(block.round, metadata, header),
      });
      renderers.renderBlock(document.querySelector('#tool'),
        {kind: 'tool', name: 'edit_file', round},
        {turn: {turnId: 'edit-summary', source: {conversationId: 'fixture-conv'}}});
    }''', {'locale': locale, 'status': status, 'failed': failed,
           'reason': FAILURE_REASON, 'hint': RECOVERY_HINT, 'theme': theme})
    # Host-only layout: do not patch any production card/row/list CSS here.
    page.add_style_tag(content='body{display:block;overflow:auto}#fixture{max-width:960px;margin:auto;padding:16px}')


@pytest.mark.parametrize('theme', ['dark', 'light', 'tofu'])
@pytest.mark.parametrize('width', [1280, 390], ids=['desktop', 'narrow'])
@pytest.mark.parametrize(('status', 'failed'), [
    ('done', True), ('error', True), ('done', False),
], ids=['partial-done', 'partial-error', 'all-success'])
def test_edit_card_count_evidence_and_compact_list(frontend_visual_page, width, status, failed, theme, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 900})
    _mount_edit_card(page, status=status, failed=failed, theme=theme)
    card = page.locator('.ptool-batch-done-block')
    header = card.locator(':scope > .ptool-batch-done-header')
    badge = header.locator('.ptool-badge')
    expected_count = '5/6' if failed else '6/6'
    assert badge.count() == 1
    assert badge.inner_text() == expected_count
    assert badge.evaluate('(node, cls) => node.classList.contains(cls)',
                          'ptool-badge-err' if failed else 'ptool-badge-ok')

    # The disclosure caret is a flex item of the summary row, so it can never
    # wrap onto a line of its own — the row is exactly the label plus padding.
    # An inline label caret (`content:" ▸"` inside the wrapping label) used to
    # add a whole caret-only line at narrow widths.
    caret = header.evaluate('''node => {
      const label = node.querySelector('.ptool-text');
      const style = getComputedStyle(node);
      return {rowContent: getComputedStyle(node, '::after').content,
        labelContent: getComputedStyle(label, '::after').content,
        rowHeight: node.getBoundingClientRect().height,
        labelHeight: label.getBoundingClientRect().height,
        padTop: parseFloat(style.paddingTop), padBottom: parseFloat(style.paddingBottom)};
    }''')
    assert caret['rowContent'] == '""', caret
    assert caret['labelContent'] == 'none', caret
    # A wrapped caret costs a FULL extra line; row min-height and the trailing
    # badge legitimately exceed label + padding by a few pixels.
    caret_overflow = caret['rowHeight'] - caret['labelHeight'] \
        - caret['padTop'] - caret['padBottom']
    assert caret_overflow <= caret['labelHeight'] / 2, caret
    # Compare computed paint to the shipped palette, not just a class. Exceptional
    # outcomes stay colored; routine success is deliberately quiet
    # (`23-reading-process.css`: "Keep exceptional outcomes colored, make routine
    # metadata readable and quiet").
    assert badge.evaluate('''(node, token) => {
      const probe = document.createElement('span');
      probe.style.color = `var(${token})`;
      node.append(probe);
      const actual = getComputedStyle(node).color;
      const expected = getComputedStyle(probe).color;
      probe.remove();
      return actual === expected && actual !== 'rgba(0, 0, 0, 0)';
    }''', '--chip-err-color' if failed else '--text-secondary')
    assert FAILURE_REASON not in header.inner_text()
    assert 'match-not-found' not in header.inner_text()
    assert header.locator('.ptool-batch-fail-reason').count() == 0
    assert not card.evaluate('node => node.open')

    header.focus()
    page.keyboard.press('Enter')
    assert card.evaluate('node => node.open')
    rows = card.locator('.ptool-batch-done-list > .ptool-batch-done-edit')
    assert rows.count() == 6
    assert card.locator('.ptool-batch-ok').count() == (5 if failed else 6)
    assert card.locator('.ptool-batch-fail').count() == (1 if failed else 0)
    if failed:
        row = card.locator('.ptool-batch-fail')
        assert row.locator('.ptool-batch-idx').inner_text() == '6'
        detail = row.locator('.ptool-batch-fail-detail')
        assert not detail.is_visible()
        row.locator('summary').focus()
        page.keyboard.press('Enter')
        assert detail.is_visible()
        assert FAILURE_REASON in detail.inner_text()
        assert RECOVERY_HINT in detail.inner_text()
        assert row.locator('.bdiff-block').count() == 0
    else:
        assert card.locator('.ptool-batch-fail-detail').count() == 0
        rows.first.locator('summary').click()
        assert rows.first.locator('.bdiff-block').is_visible()

    geometry = card.evaluate('''node => {
      const box = node.getBoundingClientRect();
      return [...node.querySelectorAll('.ptool-batch-done-list > .ptool-batch-done-edit')]
        .map(row => {
          const rect = row.getBoundingClientRect();
          return {left: rect.left - box.left, right: box.right - rect.right,
            width: rect.width, clipped: row.scrollWidth > row.clientWidth + 1};
        });
    }''')
    for row in geometry:
        # A small symmetric inset is fine; stacked body/list rails are not.
        assert 0 <= row['left'] <= 12, geometry
        assert 0 <= row['right'] <= 12, geometry
        assert abs(row['left'] - row['right']) <= 1, geometry
        assert row['width'] > 0 and not row['clipped'], geometry
    assert badge.is_visible()
    assert badge.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
    assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
    page.screenshot(path=str(tmp_path / f'edit-summary-{status}-{expected_count.replace("/", "-")}-{width}.png'))
