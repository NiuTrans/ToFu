"""Client stream ingress through real runtime, controller, rich renderers and DOM.

Uses local HTTP and synthetic network input. Timings are diagnostics: no flaky
host thresholds, server/network claims or proof of pixels actually displayed.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('count', [64, 512, 4096])
def test_client_event_reaches_dom_without_losing_reader(frontend_visual_page, count):
    page = frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js'):
        page.add_script_tag(path=str(ROOT / source))
    bundle = os.environ.get('TOFU_TURN_PIPELINE_BUNDLE') or native_module_path(
        '.native/turn-pipeline.js', ROOT / 'tests/support/turn_pipeline_browser.ts')
    page.add_script_tag(path=bundle)
    folder = Path(bundle).parent
    styles = sorted([*folder.glob("*.css"), *(folder / "assets").glob("*.css")])
    assert styles, "The production renderer CSS must accompany the fixture bundle"
    for style in styles:
        page.add_style_tag(path=str(style))
    page.evaluate('async count => { window.pipeline = await createTurnPipelineFixture(count); }', count)
    try:
        result = page.evaluate('pipeline.measure()')
        assert result['readerPreserved']
        assert result['burst']['commits'] == 1
        assert result['mountedTurns'] <= 80
        assert 'Live evidence 2000' in result['text']
        assert all(sample['commits'] == 1 for sample in result['samples'])
        result['textDigest'] = hashlib.sha256(result.pop('text').encode()).hexdigest()
        result['htmlDigest'] = hashlib.sha256(result.pop('html').encode()).hexdigest()
        destination = os.environ.get('TOFU_TURN_PIPELINE_OUTPUT')
        if destination:
            output = Path(destination); output.mkdir(parents=True, exist_ok=True)
            result.update(browser=page.context.browser.version, scope=__doc__,
                bundleSha256=hashlib.sha256(Path(bundle).read_bytes()).hexdigest(),
                cssSha256=hashlib.sha256((ROOT / 'static/styles.css').read_bytes()).hexdigest(),
                rendererCssSha256=[hashlib.sha256(style.read_bytes()).hexdigest() for style in styles])
            (output / f'{count}.json').write_text(json.dumps(result, indent=2)+'\n')
    finally:
        page.evaluate('pipeline.close(); delete window.pipeline')
    assert page.locator('#pipeline-surface').count() == 0
