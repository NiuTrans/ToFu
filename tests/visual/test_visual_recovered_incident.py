"""A refusal → fallback → switch-back incident renders once, not five times.

Local real-browser tier: the retained activity view model + renderer, the
shipped stylesheet and the locale authorities. No app/storage boot, no
provider call. The fixture is the exact entry sequence one provider refusal
emits, so the screenshot is directly comparable to the reported banner.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401
from tests._jsdom import require_node_modules

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "screenshots"
pytestmark = pytest.mark.visual

REFUSAL = (
    "kunlunbedrock.claude-opus-5.5-m17 declined the request "
    "(category: user_sensitive_words): user sensitive words detected: test_sensitive"
)

SCRIPT_PREFIX = """
({labels, language, theme}) => {
  document.documentElement.lang = language;
  document.documentElement.dataset.theme = theme;
  document.body.innerHTML = '<main id="fixture" class="conversation-surface" '
    + 'style="max-width:800px;width:100%;padding:16px;box-sizing:border-box;margin:auto"></main>';
"""

SCRIPT_BODY = """
  const localizedText = (key, fallback, values = {}) => (labels[key] || fallback)
    .replace(/\\{(\\w+)\\}/g, (match, name) => values[name] ?? match);
  const timeline = {blockId:'activity-timeline', version:1,
    entries:rows.map((row, index) => ({spanId:row.id, seq:index + 1,
      occurredAt:1000 + index, kind:'model', status:'failed', severity:'error',
      count:1, ...row}))};
  const entries = ActivityPresentation.visibleActivityEntries(timeline, [], []);
  const node = document.createElement('div');
  document.getElementById('fixture').append(node);
  for (const value of entries) {
    const block = {blockId:'activity:' + value.id, kind:'activity-event',
      identitySource:'contract', value, source:value};
    const cell = document.createElement('div');
    node.append(cell);
    ActivityRenderer.renderActivityEventBlock(cell, block,
      {localizedText, renderSafeMarkdownHtml:() => {
        throw new Error('Activity evidence is plain text');
      }});
  }
}
"""


@pytest.fixture(scope="module")
def activity_bundles(tmp_path_factory):
    require_node_modules('vite')
    out = tmp_path_factory.mktemp("recovered-incident")
    bundles = {}
    for name, module, global_name in (
        ("renderer", "frontend/src/conversation/ui/renderers/activity-block.ts",
         "ActivityRenderer"),
        ("presentation", "frontend/src/conversation/presentation/view-model/activity-blocks.ts",
         "ActivityPresentation"),
    ):
        bundle = out / f"{name}.js"
        result = subprocess.run([
            str(ROOT / "scripts/vite_test_bundle.mjs"), str(ROOT / module),
            "--bundle", "--format=iife", "--platform=browser",
            f"--global-name={global_name}", f"--outfile={bundle}",
        ], cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        bundles[name] = bundle.read_text()
    return bundles


@pytest.mark.parametrize("language,theme,width", [
    ("zh", "light", 1440), ("en", "dark", 390),
])
def test_recovered_refusal_renders_one_row_per_decision(
    frontend_visual_page, activity_bundles, language, theme, width,
):
    page = frontend_visual_page
    page.set_viewport_size({"width": width, "height": 900})
    labels = json.loads((ROOT / f"frontend/src/i18n/locales/{language}.json").read_text())
    for bundle in activity_bundles.values():
        page.add_script_tag(content=bundle)
    rows = [
        {"id": "attempt", "spanId": "model:ephemeral:1",
         "parentSpanId": "model:attempt-a:1", "requestLevel": "provider_attempt",
         "attemptScope": "dispatcher", "model": "kunlunbedrock.claude-opus-5.5-m17",
         "providerId": "example-corp", "keyHint": "2031********4861",
         "reasonCode": "ModelRefusalError", "durationMs": 3200},
        {"id": "logical", "spanId": "model:attempt-a:1", "model": "claude-opus-5.5",
         "providerId": "example-corp", "keyHint": "2031********4861",
         "summaryKey": "activity.model.requestFailed",
         "summaryArgs": {"model": "claude-opus-5.5"},
         "reasonCode": "ModelRefusalError", "detail": REFUSAL, "durationMs": 6700},
        {"id": "announce", "kind": "status", "status": "waiting",
         "severity": "warning", "phase": "retrying",
         "summaryKey": "stream.phase.refusalFallback",
         "summaryArgs": {"model": "claude-opus-5.5", "fallback": "kimi-k3.1-pro"},
         "reasonCode": "retrying", "model": "kimi-k3.1-pro"},
        {"id": "switch", "status": "switched", "severity": "warning",
         "summaryKey": "activity.model.switched",
         "summaryArgs": {"from": "claude-opus-5.5", "to": "kimi-k3.1-pro"},
         "fromModel": "claude-opus-5.5", "toModel": "kimi-k3.1-pro",
         "detail": f"content_filter: {REFUSAL}", "reasonCode": "content_filter",
         "action": "fallback"},
        {"id": "switchback", "kind": "status", "status": "waiting",
         "severity": "warning", "phase": "retrying",
         "summaryKey": "stream.phase.refusalSwitchback",
         "summaryArgs": {"model": "claude-opus-5.5", "fallback": "kimi-k3.1-pro"},
         "reasonCode": "retrying", "model": "kimi-k3.1-pro"},
    ]
    page.evaluate(SCRIPT_PREFIX + f"const rows = {json.dumps(rows)};" + SCRIPT_BODY,
                  {"labels": labels, "language": language, "theme": theme})

    # Five wire rows, two visible decisions: the recovered switch and the probe.
    assert page.locator('.activity-event').count() == 2
    assert page.locator('[data-activity-id="switch"]').count() == 1
    assert page.locator('[data-activity-id="switchback"]').count() == 1
    text = page.locator('#fixture').inner_text()
    # The refusal cause is stated exactly once, without its taxonomy prefix.
    assert text.count('test_sensitive') == 1
    assert 'content_filter' not in text
    assert 'retrying' not in text
    # The nested attempt never renders as its own request, and its 3.2s is not
    # double counted next to the enclosing 6.7s logical request.
    assert '3.2s' not in text
    assert '6.7s' in text
    # Provider/key routing evidence survives the fold.
    assert 'example-corp' in text
    assert '2031********4861' in text
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')

    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"recovered-incident-{language}-{theme}-{width}.png"),
                    full_page=True, animations="disabled")
