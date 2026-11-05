"""Studio entry, layout and evidence charts through the real source-preview server.

Synthetic receipts never authorize production execution; browser checks cover
alignment, navigation, draft protection and the shared Canvas/SVG presentation.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.evolution.test_evolution_office_browser import office_preview as office_preview  # noqa: F401
from tests.evolution.test_evolution_office_browser import hud_layout

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1024, 1536])
def test_studio_entry_layout_and_explicit_draft(browser, office_preview, width):
    context = browser.new_context(viewport={'width': width, 'height': 960}, reduced_motion='reduce')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(office_preview + '?scenario=empty')
        root = page.locator('.evo-game')
        # Without a goal the world is the entry: the banner and the one primary action invite a goal.
        expect(page.locator('.evo-quest')).to_have_attribute('data-hud', 'create')
        primary = page.locator('.evo-primary-action')
        expect(primary).to_have_attribute('data-kind', 'create')
        expect(page.locator('.evo-window')).to_be_hidden()
        assert page.locator('.evo-slot[data-place]').evaluate_all('(slots) => slots.every(slot => slot.disabled)')
        primary.click()
        field = page.locator('#evo-objective')
        expect(root).to_have_attribute('data-window', 'briefing')
        expect(field).to_be_focused()
        page.locator('[data-example=benchmark]').click()
        assert '基线' in field.input_value() or 'baseline' in field.input_value()
        draft = field.input_value()
        expect(page.locator('[data-example=research]')).to_be_disabled()
        page.locator('[data-studio-action=refresh]').evaluate('(element) => element.click()')
        expect(field).to_have_value(draft)
        layout = hud_layout(page, include_window=width > 760)
        assert not layout['overlaps'] and not layout['outside'], layout
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate('''() => {
          const field = document.querySelector('#evo-objective').getBoundingClientRect();
          return field.top >= 0 && field.bottom <= innerHeight && field.left >= 0 && field.right <= innerWidth;
        }''')
        output = Path('output/evolution-design/experience');output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / f'entry-{width}.png'), full_page=True)
        # Escape returns to the world (the draft stays), a second Escape opens the menu.
        page.keyboard.press('Escape')
        expect(page.locator('.evo-window')).to_be_hidden()
        page.keyboard.press('Escape')
        expect(root).to_have_attribute('data-window', 'menu')
        page.locator('[data-studio-route="evolution/programs"]').click()
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
        read = page.locator('.evo-primary-action[data-kind=read]')
        expect(read).to_be_visible()
        expect(page.locator('.evo-slot[data-place=review]')).to_have_attribute('data-verdict', 'pass')
        read.click()
        delivery = page.locator('.evo-tab-inspector[data-tab=delivery]')
        expect(page.locator('[role=tab][data-place=delivery]')).to_have_attribute('aria-selected', 'true')
        expect(delivery.locator('.evo-evidence-charts svg')).to_be_visible()
        expect(delivery.locator('#evo-deliverable')).to_contain_text('```tofu-ui')
        delivery.locator('.evo-evidence-charts summary').click()
        expect(delivery.locator('.evo-evidence-charts table')).to_contain_text('240')
        # The deliverable never carries the verdict; review acceptance lives in its own tab.
        assert delivery.locator('.evo-verdict').count() == 0
        page.locator('[role=tab][data-place=review]').click()
        review = page.locator('.evo-tab-inspector[data-tab=review]')
        expect(review.locator('.evo-verdict')).to_have_attribute('data-outcome', 'success')
        expect(review).to_contain_text('12 tests passed')
        assert review.locator('.evo-evidence-charts').count() == 0
    finally:
        context.close()
