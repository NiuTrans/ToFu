"""A rate-limit storm collapses to one quiet disclosure, not a red wall.

Local real-browser tier: the retained activity renderer, the shipped
stylesheet and the locale authorities. No app/storage boot, no provider call.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "screenshots"
pytestmark = pytest.mark.visual

# One failed attempt and one backoff wait, repeated — the shape a 429 storm
# produces in a live turn's activity timeline.
STORM = """
const rows = [];
for (let attempt = 1; attempt <= 6; attempt += 1) {
  rows.push({id:`failed-${attempt}`, kind:'model', status:'failed',
    statusCode:429, model:'deepseek-v4.1-flash-m17', durationMs:556,
    summary:'deepseek-v4.1-flash-m17 Request failed',
    detail:'HTTP 429 · RateLimitError'});
  rows.push({id:`wait-${attempt}`, kind:'status', status:'retrying',
    severity:'warning', phase:'retrying', attempt, model:'deepseek-v4.1-flash-m17',
    durationMs:11000, count:3,
    summary:'Model deepseek-v4.1-flash-m17 is rate-limited, queuing for retry ' +
      `(Attempt ${attempt})…`});
}
"""


SCRIPT_PREFIX = """
({labels, language, theme}) => {
  document.documentElement.lang = language;
  document.documentElement.dataset.theme = theme;
  document.body.innerHTML = '<main id="fixture" class="conversation-surface" '
    + 'style="max-width:800px;width:100%;padding:16px;box-sizing:border-box;margin:auto"></main>';
"""

# A leading glyph must share its label's first line box. A fixed
# margin-block-start on a 16px box drifts 4px low against the 12px/1.45 meta
# line, which is what the retry row looked like before this pin existed.
ALIGNMENT_PROBE = """
(selector) => {
  const out = [];
  for (const row of document.querySelectorAll(selector)) {
    const marker = row.querySelector('.activity-event__marker, .activity-event-group__marker');
    const text = row.querySelector('.activity-event__summary, .activity-event-group__label');
    if (!marker || !text) continue;
    const range = document.createRange();
    range.selectNodeContents(text);
    // A wrapped label has several line boxes; the glyph belongs to the first.
    const glyph = range.getClientRects()[0];
    const box = marker.getBoundingClientRect();
    out.push({
      text: text.textContent.slice(0, 24),
      delta: (box.top + box.height / 2) - (glyph.top + glyph.bottom) / 2,
    });
  }
  return out;
}
"""


# Chromium's CJK fallback reports a glyph box ~0.7px above the line box center;
# 1.5px still fails the old 4px offset with room to spare.
ALIGNMENT_TOLERANCE_PX = 1.5

SCRIPT_BODY = """
  const localizedText = (key, fallback, values = {}) => (labels[key] || fallback)
    .replace(/\\{(\\w+)\\}/g, (match, name) => values[name] ?? match);
  const items = rows.map((row, index) => {
    const entry = {spanId:row.id, seq:index + 1, occurredAt:1000 + index,
      severity:'error', count:1, ...row};
    return {blockId:'activity:' + row.id, kind:'activity-event',
      identitySource:'contract', value:entry, source:entry};
  });
  const node = document.createElement('div');
  document.getElementById('fixture').append(node);
  ActivityRenderer.renderActivityEventGroupBlock(node, {
    blockId:items[0].blockId, kind:'activity-event-group',
    identitySource:'contract', source:items[0].value, items,
  }, {localizedText, renderSafeMarkdownHtml:() => {
    throw new Error('Activity evidence is plain text');
  }});
}
"""


@pytest.fixture(scope="module")
def activity_renderer_bundle(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("retry-storm") / "activity-renderer.js"
    result = subprocess.run([
        str(ROOT / "scripts/vite_test_bundle.mjs"),
        str(ROOT / "frontend/src/conversation/ui/renderers/activity-block.ts"),
        "--bundle", "--format=iife", "--platform=browser",
        "--global-name=ActivityRenderer", f"--outfile={bundle}",
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return bundle.read_text()


@pytest.mark.parametrize("language,theme,width", [
    ("en", "light", 1440), ("zh", "dark", 390),
])
def test_retry_storm_renders_as_one_collapsed_disclosure(
    frontend_visual_page, activity_renderer_bundle, language, theme, width,
):
    page = frontend_visual_page
    page.set_viewport_size({"width": width, "height": 900})
    labels = json.loads((ROOT / f"frontend/src/i18n/locales/{language}.json").read_text())
    page.add_script_tag(content=activity_renderer_bundle)
    page.evaluate(SCRIPT_PREFIX + STORM + SCRIPT_BODY,
                  {"labels": labels, "language": language, "theme": theme})

    fold = page.locator('details.activity-event-group')
    summary = fold.locator('summary')
    expected_count = labels['activity.retryGroup.summary'].replace('{count}', '12')
    assert summary.inner_text().split('\n')[0] == expected_count
    assert summary.inner_text().count('deepseek-v4.1-flash-m17') == 1
    assert not fold.evaluate('node => node.open')
    # Collapsed means one row on screen, not twelve.
    assert fold.locator('.activity-event:visible').count() == 0
    collapsed_height = fold.bounding_box()['height']
    assert collapsed_height <= 48, collapsed_height

    summary_rows = page.evaluate(ALIGNMENT_PROBE, 'details.activity-event-group')
    assert len(summary_rows) == 1
    assert abs(summary_rows[0]['delta']) <= ALIGNMENT_TOLERANCE_PX, summary_rows

    summary.focus()
    page.keyboard.press('Enter')
    assert fold.evaluate('node => node.open')
    assert summary.evaluate('node => node === document.activeElement')
    assert fold.locator('.activity-event').count() == 12
    assert fold.locator('.activity-event:visible').count() == 12

    member_rows = page.evaluate(ALIGNMENT_PROBE, '.activity-event')
    assert len(member_rows) == 12
    worst = max(abs(row['delta']) for row in member_rows)
    assert worst <= ALIGNMENT_TOLERANCE_PX, member_rows
    expanded_height = fold.bounding_box()['height']
    assert expanded_height > collapsed_height * 4
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"retry-storm-{language}-{theme}-{width}"
    page.screenshot(path=str(OUT / f"{stem}-expanded.png"), full_page=True,
                    animations="disabled")
    page.keyboard.press('Space')
    assert not fold.evaluate('node => node.open')
    assert fold.bounding_box()['height'] == pytest.approx(collapsed_height, abs=1)
    page.screenshot(path=str(OUT / f"{stem}-collapsed.png"), full_page=True,
                    animations="disabled")
