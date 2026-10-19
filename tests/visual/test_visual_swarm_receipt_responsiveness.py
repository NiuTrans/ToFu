"""Receipt-heavy history must preserve live input and event-to-DOM delivery.

Uses a local HTTP server and actual runtime, overlay, controller and renderers.
The matching unit contract bounds transcript parses/copies; browser clocks are
diagnostics rather than machine-dependent latency promises.
"""
import json
from pathlib import Path

import pytest

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import frontend_visual_origin, frontend_visual_page as frontend_visual_page  # noqa: F401

pytestmark = pytest.mark.visual


def test_stream_with_large_outputs_and_repeated_agent_receipts(frontend_visual_page):
    page = frontend_visual_page
    for source in ('node_modules/marked/lib/marked.umd.js', 'node_modules/dompurify/dist/purify.min.js'):
        page.add_script_tag(path=str(ROOT / source))
    bundle = native_module_path('.native/swarm-receipt-pipeline.js', ROOT / 'tests/support/turn_pipeline_browser.ts')
    page.add_script_tag(path=bundle)
    folder = Path(bundle).parent
    styles = sorted([*folder.glob('*.css'), *(folder / 'assets').glob('*.css')])
    assert styles, 'Production renderer CSS must accompany the fixture bundle'
    for style in styles:
        page.add_style_tag(path=str(style))
    page.evaluate('async () => { window.pipeline = await createTurnPipelineFixture(4, 32); }')
    try:
        result = page.evaluate('pipeline.measure(12)')
        assert result['readerPreserved']
        assert result['burst']['commits'] == 1
        assert 'Live evidence 2000' in result['text']
        assert result['mountedTurns'] <= 4
        assert all(sample['commits'] == 1 for sample in result['samples'])
        print(json.dumps({
            'initialMs': result['initialMs'],
            'maxDispatchMs': max(sample['dispatchMs'] for sample in result['samples']),
            'burst': result['burst'], 'terminal': result['terminal'],
        }))
    finally:
        page.evaluate('pipeline.close(); delete window.pipeline')
    assert page.locator('#pipeline-surface').count() == 0
