"""Chromium updates a program card from actual mid-run gateway projections."""
import copy
import re

import pytest

from lib.tasks_pkg.orchestrator import _programmatic
from tests._runtime_sections import native_module_path
from tests.support.frontend_visual import ROOT

pytestmark = pytest.mark.visual
pytest_plugins = ('tests.tasks_pkg.test_javascript_dependency_chains',
                  'tests.support.frontend_visual')


@pytest.mark.parametrize('width', [390, 1280])
def test_live_children_update_without_resetting_source_disclosure_or_focus(
        local_gateway, monkeypatch, frontend_visual_page, width, tmp_path):
    task, _, invoke = local_gateway
    snapshots = []
    append = _programmatic.append_event

    def collect(carrier, event):
        if event['type'] in ('program_progress', 'program_output'):
            snapshots.append(copy.deepcopy(next(row for row in carrier['toolRounds']
                                               if row.get('_programSynthetic'))))
        append(carrier, event)

    monkeypatch.setattr(_programmatic, 'append_event', collect)
    tool = {'type': 'function', 'function': {'name': 'read', 'parameters': {
            'type': 'object', 'properties': {}, 'additionalProperties': False}}}
    invoke("await tools.invoke('read',{}); await tools.invoke('read',{}); return {total:12};",
           [tool], {'read': lambda args: ('done', {'value': 6})})
    assert [len(row['childCallIds']) for row in snapshots] == [1, 2, 2]
    page = frontend_visual_page
    page.set_viewport_size({'width': width, 'height': 880})
    page.evaluate("document.body.innerHTML='<main id=program></main>'")
    page.add_style_tag(content="body{display:block;overflow:auto;height:auto}#program{max-width:100%;min-width:0}")
    page.add_script_tag(path=native_module_path('.native/program-progress-card.js',
                         ROOT / 'frontend/src/conversation/ui/program-execution-card.ts'))
    page.evaluate("row=>renderProgramExecutionCard(document.getElementById('program'),row)", snapshots[0])
    card = page.locator('.ptc-card')
    assert card.locator('.ptc-status').inner_text() == 'Executing program'
    assert re.match(r'^1\b', card.locator('.ptc-meta').inner_text())
    card.locator('.ptc-code summary').click()
    assert card.locator('.ptc-code').evaluate('node=>node.open')
    page.evaluate("row=>renderProgramExecutionCard(document.getElementById('program'),row)", snapshots[1])
    assert re.match(r'^2\b', card.locator('.ptc-meta').inner_text())
    assert card.locator('.ptc-status').inner_text() == 'Executing program'
    assert card.locator('.ptc-code').evaluate('node=>node.open')
    assert card.locator('.ptc-code summary').evaluate('node=>node===document.activeElement')
    assert card.locator('.ptc-result').count() == 0
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    assert card.locator('.ptc-status').evaluate('node=>node.getBoundingClientRect().right<=window.innerWidth')
    assert card.evaluate('node=>node.getBoundingClientRect().width<=window.innerWidth')
    page.screenshot(path=str(tmp_path / f'progress-{width}.png'))
    page.evaluate("row=>renderProgramExecutionCard(document.getElementById('program'),row)", snapshots[-1])
    assert card.locator('.ptc-status').inner_text() == 'Completed'
    assert '12' in card.locator('.ptc-result').inner_text()
    assert card.locator('.ptc-code').evaluate('node=>node.open')
