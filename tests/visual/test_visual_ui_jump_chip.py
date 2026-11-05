"""Real Chromium: an in-message UI-jump chip sits centred on its prose line.

The assistant's ``setting:<key>`` / ``page:<key>`` links render as
``.md-ui-jump`` buttons inside Markdown prose (markdown/html-policy.ts). A
fixed 11px label left on the prose baseline sank the pill below the line — its
box centre sat 2.3px under the neighbouring text at the 16px reading size — and
a 1px side margin let it touch adjacent CJK text, which has no word spaces.
"""
import pytest
from playwright.sync_api import expect

from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def _chip(action, key, label):
    # Same markup projectMarkdownHtml emits for <a href="page:update">…</a>.
    return f'<button type="button" class="md-ui-jump" data-tofu-action="{action}(\'{key}\')">{label}</button>'


# Short enough to stay on one line at a 390px viewport, chip included.
SAMPLES = {
    'zh': ('从', _chip('jumpToPage', 'update', '软件更新'), '重启即可。'),
    'en': ('Open ', _chip('jumpToSetting', 'general', 'General'), ' now.'),
}
# (container, single-line host) per Markdown surface.
CONTEXTS = {
    # Conversation answer prose: the 16px reading size.
    'conversation': ('<div class="conversation-surface"><div class="conversation-block conversation-block--text">'
                     '<div class="md-content">{lines}</div></div></div>', '<p {attr}>{text}</p>'),
    # Other Markdown surfaces keep the 14.5px .md-content default.
    'markdown': ('<div class="md-content">{lines}</div>', '<p {attr}>{text}</p>'),
    # Table cells: 13px on desktop and 12px on mobile, where the label sits on its 11px floor.
    'table': ('<div class="md-content">{lines}</div>',
              '<table><tbody><tr><td {attr}>{text}</td></tr></tbody></table>'),
}

MEASURE = r"""() => [...document.querySelectorAll('[data-sample]')].map(host => {
  const chip = host.querySelector('.md-ui-jump');
  const ref = document.querySelector(`[data-ref="${host.dataset.sample}"]`);
  const charRect = (node, index) => {
    const range = document.createRange();
    range.setStart(node, index);
    range.setEnd(node, index + 1);
    return range.getBoundingClientRect();
  };
  const before = chip.previousSibling, after = chip.nextSibling;
  const prev = charRect(before, before.data.trimEnd().length - 1);
  const next = charRect(after, after.data.length - after.data.trimStart().length);
  const box = chip.getBoundingClientRect();
  const centre = rect => (rect.top + rect.bottom) / 2;
  return {
    sample: host.dataset.sample,
    labelSize: parseFloat(getComputedStyle(chip).fontSize),
    proseSize: parseFloat(getComputedStyle(host).fontSize),
    boxCentreOffset: centre(box) - (centre(prev) + centre(next)) / 2,
    gapBefore: box.left - prev.right,
    gapAfter: next.left - box.right,
    hostHeight: host.getBoundingClientRect().height,
    refHeight: ref.getBoundingClientRect().height,
  };
})"""


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width', [390, 1280])
def test_ui_jump_chip_is_centred_on_its_line_with_side_space(frontend_visual_page, theme, width, tmp_path):
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 700})
    # The application bundle always ships these faces; their metrics set the line geometry.
    page.add_style_tag(url='/frontend/src/styles/fonts.css')
    html = []
    for context, (container, line) in CONTEXTS.items():
        lines = []
        for name, (before, chip, after) in SAMPLES.items():
            sample = f'{context}-{name}'
            lines.append(line.format(attr=f'data-sample="{sample}"', text=before + chip + after))
            lines.append(line.format(attr=f'data-ref="{sample}"', text=before + after))
        html.append(container.format(lines=''.join(lines)))
    page.evaluate("""async ([theme, html]) => {
      document.documentElement.setAttribute('data-theme', theme);
      document.body.style.cssText = 'display:block;overflow:auto';
      document.body.innerHTML = '<main style="margin:24px;max-width:760px">' + html + '</main>';
      await document.fonts.ready;
    }""", [theme, ''.join(html)])
    rows = page.evaluate(MEASURE)
    assert {row['sample'] for row in rows} == {f'{c}-{s}' for c in CONTEXTS for s in SAMPLES}
    for row in rows:
        # A smaller label marks it as an app action, but never below the type scale floor (11px).
        assert 11 <= row['labelSize'] < row['proseSize'], row
        if row['sample'].endswith('-zh'):
            # CJK prose has no spaces, so the chip itself keeps text off its edges.
            assert row['gapBefore'] >= 3 and row['gapAfter'] >= 3, row
        if row['proseSize'] < 13:
            # 12px mobile table cells: the 11px label floor leaves no room to centre
            # the pill inside the line, so only spacing and legibility hold there.
            continue
        # Centred on the neighbouring text: the pill neither sinks below nor floats above the line.
        assert abs(row['boxCentreOffset']) <= 1.0, row
        # It fits the existing line box instead of stretching that line.
        assert abs(row['hostHeight'] - row['refHeight']) < 0.01, row
    # The arrow is decoration: the button is announced by its label alone.
    for chip in page.locator('.md-ui-jump').all():
        expect(chip).to_have_accessible_name(chip.text_content())
    page.screenshot(path=str(tmp_path / f'ui-jump-chip-{theme}-{width}.png'))
