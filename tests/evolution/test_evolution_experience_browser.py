"""Studio entry, layout and evidence charts through the real source-preview server.

Synthetic receipts never authorize production execution; browser checks cover
alignment, navigation, draft protection and the shared Canvas/SVG presentation.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.evolution.test_evolution_office_browser import office_preview as office_preview  # noqa: F401

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1024, 1536])
def test_studio_entry_layout_and_explicit_draft(browser, office_preview, width):
    context = browser.new_context(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(office_preview + '?scenario=empty')
        field = page.locator('#evo-objective')
        expect(field).to_be_visible()
        page.locator('[data-example=benchmark]').click()
        assert '基线' in field.input_value() or 'baseline' in field.input_value()
        draft = field.input_value()
        expect(page.locator('[data-example=research]')).to_be_disabled()
        page.locator('[data-studio-action=refresh]').evaluate('(element) => element.click()')
        expect(field).to_have_value(draft)
        assert page.locator('wa-drawer[open]').count() == 0
        head = page.locator('.evo-ledger-head').bounding_box()
        actions = page.locator('.evo-studio-actions').bounding_box()
        assert head and actions
        assert head['x'] + head['width'] <= actions['x'] + 1 or head['y'] + head['height'] <= actions['y'] + 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if width == 390:
            assert field.bounding_box()['y'] < page.locator('.evo-studio-grid').bounding_box()['y']
        output = Path('output/evolution-design/experience');output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'entry-{width}.png'), full_page=True)
        page.locator('[data-studio-action=notebook]').click()
        expect(page.locator('.evo-programs')).to_be_visible()
        assert not errors
    finally:
        context.close()


@pytest.mark.parametrize('width', [390, 1536])
def test_experiment_charts_switch_metric_and_keep_original_values(browser, office_preview, width):
    context = browser.new_context(viewport={'width': width, 'height': 960})
    page = context.new_page()
    try:
        page.goto(office_preview + '?scenario=program')
        chart = page.locator('.exp-observations')
        expect(chart.locator('svg')).to_be_visible()
        chart.locator('[data-chart-metric=wall_clock_seconds]').click()
        expect(chart.locator('[data-chart-metric=wall_clock_seconds]')).to_have_attribute('aria-pressed', 'true')
        chart.locator('[data-chart-kind=line]').click()
        expect(chart.locator('.rich-data-chart-line')).to_have_count(2)
        chart.locator('.exp-chart-data summary').click()
        expect(chart.locator('table')).to_be_visible()
        expect(chart.locator('tbody tr')).to_have_count(7)
        expect(chart.locator('table')).to_contain_text('≥ #3')
        expect(chart.locator('table')).to_contain_text('120')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        output = Path('output/evolution-design/experience');output.mkdir(parents=True, exist_ok=True)
        chart.screenshot(path=str(output / f'observations-{width}.png'))
    finally:
        context.close()


def test_deliverable_chart_keeps_raw_evidence_and_review_separate(browser, office_preview):
    context = browser.new_context(viewport={'width': 1536, 'height': 960})
    page = context.new_page()
    try:
        page.goto(office_preview + '?scenario=terminal')
        page.locator('.evo-ledger-head').click()
        drawer = page.locator('#evo-detail-drawer')
        expect(drawer.locator('.evo-evidence-charts svg')).to_be_visible()
        expect(drawer.locator('#evo-deliverable')).to_contain_text('```tofu-ui')
        drawer.locator('.evo-evidence-charts summary').click()
        expect(drawer.locator('.evo-evidence-charts table')).to_contain_text('240')
        expect(drawer.locator('.evo-chart-note')).to_be_visible()
    finally:
        context.close()
