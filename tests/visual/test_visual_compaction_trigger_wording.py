"""Compaction causes stay distinct and readable without blaming the summary model.

Local real-browser tier: the retained activity renderer, shipped stylesheet and
locale authorities; no app/storage boot or provider calls.
"""
import json
import subprocess
from pathlib import Path

import pytest

from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "screenshots"
pytestmark = pytest.mark.visual


@pytest.fixture(scope="module")
def activity_renderer_bundle(tmp_path_factory):
    bundle = tmp_path_factory.mktemp("compaction-trigger") / "activity-renderer.js"
    result = subprocess.run([
        str(ROOT / "scripts/vite_test_bundle.mjs"),
        str(ROOT / "frontend/src/conversation/ui/renderers/activity-block.ts"),
        "--bundle", "--format=iife", "--platform=browser",
        "--global-name=ActivityRenderer", f"--outfile={bundle}",
    ], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return bundle.read_text()


@pytest.mark.parametrize("language,theme,width", [
    ("en", "light", 1440), ("en", "dark", 390),
    ("zh", "light", 1440), ("zh", "dark", 390),
])
def test_visual_compaction_trigger_wording(
    frontend_visual_page, activity_renderer_bundle, language, theme, width,
):
    page = frontend_visual_page
    page.set_viewport_size({"width": width, "height": 1000})
    labels = json.loads((ROOT / f"frontend/src/i18n/locales/{language}.json").read_text())
    page.add_script_tag(content=activity_renderer_bundle)
    page.evaluate(r"""({labels,language,theme}) => {
      document.documentElement.lang=language;
      document.documentElement.dataset.theme=theme;
      document.body.innerHTML='<main id="fixture" class="conversation-surface" style="max-width:800px;width:100%;padding:16px;box-sizing:border-box;margin:auto"></main>';
      const localizedText=(key,fallback,values={}) => (labels[key]||fallback)
        .replace(/\{(\w+)\}/g, (match,name)=>values[name]??match);
      for (const [index,trigger] of ['window','dispatch_guard','context_overflow'].entries()) {
        const node=document.createElement('div');
        node.className='conversation-block conversation-block--activity-event';
        node.dataset.trigger=trigger;
        document.getElementById('fixture').append(node);
        const entry={id:`compaction-${index}`, spanId:`compaction-${index}`, seq:index+1,
          occurredAt:1000+index, kind:'status', status:'succeeded', severity:'info',
          summary:'Context compacted', summaryKey:'activity.compaction.succeeded',
          count:1, reasonCode:'context_compaction', trigger,
          detail:`Recorded trigger: ${trigger}`, model:'Sol', taskModel:'Sol', summaryModel:'Luna',
          summarySelectedModel:'Luna', summaryProviderId:'summary-provider',
          archiveId:`archive-${index}`, tokensBefore:280000, tokensAfter:35000,
          durationMs:42000};
        ActivityRenderer.renderActivityEventBlock(node, {
          kind:'activity-event', blockId:entry.id, identitySource:'contract', value:entry, source:entry,
        }, {localizedText, renderSafeMarkdownHtml:()=>{throw new Error('Plain-text activity only');}});
      }
    }""", {"labels": labels, "language": language, "theme": theme})

    triggers = {
        "window": "window",
        "dispatch_guard": "dispatchGuard",
        "context_overflow": "contextOverflow",
    }
    summary_label = labels["activity.compaction.summaryModel"].replace("{model}", "Luna")
    main_label = labels["activity.compaction.taskModel"].replace("{model}", "Sol")
    rendered_causes = []
    for trigger, key in triggers.items():
        card = page.locator(f'[data-trigger="{trigger}"]')
        facts = card.locator('.activity-event__body > .activity-event__facts')
        cause = labels[f"activity.compaction.trigger.{key}"]
        rendered_causes.append(cause)
        assert facts.inner_text() == f"{summary_label} · {cause}"
        assert "Sol" not in facts.inner_text()
        assert card.locator('.activity-event__summary').inner_text() == labels['activity.compaction.succeeded']
        assert not card.locator('details').evaluate('node=>node.open')
        assert not card.locator('details .activity-event__detail').is_visible()
        assert card.locator('button[data-conversation-action="open-compaction"]').is_visible()
        assert facts.evaluate('node=>node.scrollWidth<=node.clientWidth+1'), "Trigger label is clipped"
    assert len(set(rendered_causes)) == 3
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')

    OUT.mkdir(parents=True, exist_ok=True)
    stem = f"compaction-triggers-{language}-{theme}-{width}"
    page.screenshot(path=str(OUT / f"{stem}-collapsed.png"), full_page=True, animations="disabled")
    for trigger in triggers:
        card = page.locator(f'[data-trigger="{trigger}"]')
        fold = card.locator('details')
        fold.locator('summary').focus()
        page.keyboard.press('Enter')
        assert fold.evaluate('node=>node.open')
        assert fold.locator('.activity-event__detail').inner_text() == f"Recorded trigger: {trigger}"
        assert main_label in fold.inner_text()
        assert 'summary-provider' in fold.inner_text()
        assert fold.locator('summary').evaluate('node=>node===document.activeElement')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(OUT / f"{stem}-expanded.png"), full_page=True, animations="disabled")
    for trigger in triggers:
        fold = page.locator(f'[data-trigger="{trigger}"] details')
        fold.locator('summary').focus()
        page.keyboard.press('Space')
        assert not fold.evaluate('node=>node.open')
