"""Run the isolated design study in Chromium; no application or external APIs.

Usage: python3 designs/research-workbench/verify.py
Screenshots and bounded results are local design deliverables, not research data.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from chromium_env import ensure_chromium_env
from playwright.sync_api import sync_playwright


def verify_literature(page, output, checks):
    """Exercise shared projections; sorting/filtering must never alter evidence scope."""
    page.locator('.tabs [data-page="evidence"]').click()
    assert page.locator('#context').is_hidden()
    assert page.get_by_role('table').count() == 1

    geometry = page.locator('.literature-table').evaluate('''(el) => ({
      width: el.clientWidth, scrollWidth: el.scrollWidth,
      rowHeights: [...el.querySelectorAll('tbody tr')].map(row => Math.round(row.getBoundingClientRect().height)),
      borderRadius: getComputedStyle(el).borderRadius
    })''')
    assert geometry['scrollWidth'] <= geometry['width'] + 1, geometry
    assert geometry['borderRadius'] == '0px', geometry
    assert max(geometry['rowHeights']) <= 160, geometry
    checks.append('Desktop literature geometry: ' + json.dumps(geometry))
    ids = lambda: page.locator('#evidence-rows tr[data-paper-id]').evaluate_all('(rows) => rows.map(row => row.dataset.paperId)')
    assert ids() == ['A', 'B', 'C', 'D']
    assert page.locator('#evidence-rows [data-paper-id="C"] .citation-number').inner_text() == '0'
    assert page.locator('#evidence-rows [data-paper-id="D"] .citation-number').inner_text() == '—'
    year_tie = page.evaluate("['结构化反馈与工具错误修复','推理预算匹配后的反思对照'].sort((a,b)=>a.localeCompare(b,'zh-CN')).map(title=>title.startsWith('结构')?'A':'B')")
    for value, expected in [
        ('citations-desc', ['B', 'A', 'C', 'D']),
        ('citations-asc', ['C', 'A', 'B', 'D']),
        ('year-desc', year_tie + ['C', 'D']),
        ('year-asc', ['C'] + year_tie + ['D']),
        ('relevance', ['A', 'B', 'C', 'D']),
    ]:
        page.locator('#paper-sort').select_option(value)
        assert ids() == expected, (value, ids())
        assert page.locator('#scope-count').inner_text() == '4 条在当前选择中'
        assert page.locator('#scope-draft').is_hidden()
        assert page.locator('th[aria-sort]').count() == (0 if value == 'relevance' else 1)
    # Native button keyboard activation and focused header survives DOM updates.
    header = page.locator('[data-sort-key="citations"]')
    header.focus()
    page.keyboard.press('Enter')
    assert ids() == ['B', 'A', 'C', 'D']
    assert page.locator('[data-sort-column="citations"]').get_attribute('aria-sort') == 'descending'
    assert header.evaluate('(el)=>el===document.activeElement')
    page.keyboard.press('Space')
    assert ids() == ['C', 'A', 'B', 'D']
    assert page.locator('[data-sort-column="citations"]').get_attribute('aria-sort') == 'ascending'
    checks.append('Numeric year/citation sorting both directions; stable ties; null last and zero retained; keyboard aria-sort')

    page.locator('#venue-filter').select_option('top')
    assert ids() == ['A']
    page.locator('#venue-filter').select_option('workshop')
    assert ids() == ['B']
    page.locator('[data-paper-id="B"] [data-metadata="B"]').first.click()
    dialog = page.get_by_role('dialog')
    assert dialog.get_by_text('合成元数据 · 无在线核验', exact=True).is_visible()
    assert 'Workshop' in dialog.inner_text() and '非真实' not in dialog.get_by_role('heading').inner_text()
    assert '不是这些示例论文的真实发表记录' in dialog.inner_text()
    assert 'Semantic Scholar' in dialog.inner_text() and '2026-09-07' in dialog.inner_text()
    assert dialog.locator('a[href]').count() == 0  # Do not fabricate verification links.
    page.screenshot(path=str(output / '11-bibliography-provenance.png'), full_page=True)
    page.keyboard.press('Escape')
    assert page.locator('[data-paper-id="B"] [data-metadata="B"]').first.evaluate('(el)=>el===document.activeElement')
    page.locator('#venue-filter').select_option('unverified')
    assert ids() == ['C', 'D']
    page.locator('[data-paper-id="D"] [data-metadata="D"]').first.click()
    assert '未知，不能当作 0' in dialog.inner_text() and '尚未核验' in dialog.inner_text()
    page.keyboard.press('Escape')
    page.locator('#relation-filter').select_option('context')
    assert ids() == ['C']
    page.locator('#relation-filter').select_option('all')
    page.locator('#venue-filter').select_option('all')
    search = page.get_by_role('searchbox', name='筛选示例文献')
    search.fill('  iclr  ')
    assert ids() == ['A'] and search.evaluate('(el)=>el===document.activeElement')
    search.fill('')
    page.locator('#venue-filter').select_option('top')
    page.get_by_role('button', name='顶会范围', exact=True).click()
    page.locator('[data-venue-option="ICLR"]').uncheck()
    page.get_by_role('button', name='应用演示范围', exact=True).click()
    assert ids() == []
    assert page.get_by_text('没有匹配的示例文献').is_visible()
    assert page.locator('#scope-count').inner_text() == '4 条在当前选择中'
    page.get_by_role('button', name='顶会范围', exact=True).click()
    page.locator('[data-venue-option="ICLR"]').check()
    page.get_by_role('button', name='应用演示范围', exact=True).click()
    assert ids() == ['A']
    page.locator('#venue-filter').select_option('all')
    page.locator('#paper-sort').select_option('citations-desc')
    page.locator('[data-evidence-view="comparison"]').click()
    assert ids() == ['B', 'A', 'C', 'D']
    assert page.get_by_role('columnheader', name='预算可比性').is_visible()
    assert page.locator('th[aria-sort]').count() == 0
    page.screenshot(path=str(output / '13-evidence-comparison.png'), full_page=True)
    page.locator('[data-evidence-view="literature"]').click()
    assert ids() == ['B', 'A', 'C', 'D']
    checks.append('Venue/role/search composition, workshop distinction, configurable top-main policy, provenance and shared views')

    # Compact list stays a table on mobile; avoid pinned columns hiding metadata.
    page.set_viewport_size({'width': 390, 'height': 844})
    page.screenshot(path=str(output / '12-literature-mobile.png'), full_page=True)
    region = page.get_by_role('region', name='可横向滚动的证据比较表')
    assert region.evaluate('(el)=>el.scrollWidth>el.clientWidth')
    region.focus()
    page.keyboard.press('ArrowRight')
    region.evaluate('(el)=>el.scrollLeft=el.scrollWidth')
    assert region.evaluate('(el)=>el.scrollLeft>0')
    assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
    # All right-side metadata can be exposed; title remains available by scrolling back.
    citation = page.locator('[data-paper-id="B"] .citation-number')
    citation.click()
    assert dialog.is_visible() and '1,204' in dialog.inner_text()
    page.keyboard.press('Escape')
    region.evaluate('(el)=>el.scrollLeft=0')
    page.locator('[data-paper-id="A"] [data-source="A"]').click()
    assert dialog.is_visible()
    assert '128' in dialog.inner_text() and 'ICLR' in dialog.inner_text()
    page.keyboard.press('Escape')
    assert page.locator('[data-source="A"]').first.evaluate('(el)=>el===document.activeElement')
    checks.append('Mobile native table with local scroll, accessible citation/title controls and metadata-bearing source dialog')
    page.set_viewport_size({'width': 1440, 'height': 1100})
    page.locator('#scenario').select_option('synthesis')
    page.locator('.tabs [data-page="evidence"]').click()


def run():
    ensure_chromium_env()
    output = HERE / 'previews'
    output.mkdir(exist_ok=True)
    html = (HERE / 'index.html').read_text()
    html = html.replace('<link rel="stylesheet" href="prototype.css">', '<style>' + (HERE / 'prototype.css').read_text() + '</style>')
    html = html.replace('<script src="prototype.js" defer></script>', '')
    script = (HERE / 'prototype.js').read_text()
    checks = []
    errors = []
    requests = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1100}, reduced_motion='reduce')
        context.route('**/*', lambda route: (requests.append(route.request.url), route.abort()))
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content(html)
        page.add_script_tag(content=script)
        page.get_by_role('heading', name='先定位错误，再决定如何恢复。').wait_for()
        checks.append('Initial render')
        # Page navigation, localized layout and no document-level horizontal overflow.
        for width in [390, 768, 1024, 1440, 1920]:
            page.set_viewport_size({'width': width, 'height': 1000})
            for name in ['brief', 'evidence', 'hypotheses', 'experiments', 'outputs', 'home']:
                page.locator(f'.tabs [data-page="{name}"]').click() if name != 'home' else page.locator('.breadcrumb [data-page="home"]').click()
                page.wait_for_timeout(30)
                overflow = page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
                assert not overflow, (width, name, 'document overflow')
                assert page.locator('main h1').count() == 1, (width, name, 'heading count')
            checks.append(f'6 views at {width}px: no document overflow')
        page.set_viewport_size({'width': 1440, 'height': 1100})
        page.locator('.tabs [data-page="brief"]').click()
        page.screenshot(path=str(output / '01-brief-desktop.png'), full_page=True)
        # Inspector is nonmodal on desktop, and returns focus to its source trigger.
        trigger = page.locator('.judgment [data-source="A"]')
        trigger.click()
        assert page.locator('#context').get_by_text('示例文献 · 非真实论文', exact=True).is_visible()
        assert page.locator('#inspector-heading').evaluate('(el) => document.activeElement === el')
        page.screenshot(path=str(output / '02-evidence-inspector.png'), full_page=True)
        page.keyboard.press('Escape')
        assert trigger.evaluate('(el) => document.activeElement === el')
        checks.append('Desktop evidence inspector focus and Escape restoration')
        verify_literature(page, output, checks)
        # Filter retains focused input; source selection explicitly marks stale synthesis.
        page.locator('.tabs [data-page="evidence"]').click()
        search = page.get_by_role('searchbox', name='筛选示例文献')
        search.fill('不存在的研究标题')
        assert page.get_by_text('没有匹配的示例文献').is_visible()
        assert search.evaluate('(el) => document.activeElement === el')
        page.get_by_role('button', name='清除筛选', exact=True).click()
        page.get_by_role('checkbox', name='将示例文献 B 纳入来源范围').uncheck()
        assert page.get_by_text('来源范围已修改，简报尚未重新核验。').is_visible()

        page.locator('.tabs [data-page="brief"]').click()
        assert page.get_by_text('需要重新核验 · 旧版判断').is_visible()
        page.locator('.tabs [data-page="experiments"]').click()
        page.get_by_role('button', name='审阅并批准实验').click()
        assert page.get_by_role('heading', name='先重新核验实验依据').is_visible()
        page.keyboard.press('Escape')
        page.locator('.tabs [data-page="evidence"]').click()
        page.get_by_role('button', name='还原来源选择').click()
        page.screenshot(path=str(output / '03-evidence-table.png'), full_page=True)
        checks.append('Evidence filter, empty result, explicit source-scope revalidation')
        # Approval grants only a simulated queue transition, no scientific success.
        page.locator('.tabs [data-page="experiments"]').click()
        page.locator('#scenario').select_option('approval')
        
        page.get_by_role('button', name='审阅并批准实验').click()
        confirm = page.get_by_role('button', name='模拟批准并排队 →')
        assert confirm.is_disabled()
        page.screenshot(path=str(output / '04-approval.png'), full_page=True)
        page.locator('#approval-check').check()
        assert confirm.is_enabled()
        confirm.click()
        assert page.locator('main').get_by_text('已模拟排队 · 尚无结果').is_visible()

        assert '等待批准' not in page.locator('#run-status').inner_text()
        assert page.get_by_text('单次确认已模拟记录，任务已模拟排队。').is_visible()
        assert page.locator('main').get_by_text('科学结果：').is_visible()
        assert page.locator('#modal').evaluate('(el) => !el.open')
        checks.append('Approval defaults denied; explicit scope; queued is not successful science')
        # Modal keyboard confinement and restoration.
        page.locator('.topbar [data-action="commands"]').click()
        for _ in range(12):
            page.keyboard.press('Tab')
            assert page.evaluate('document.querySelector("#modal").contains(document.activeElement)')
        page.keyboard.press('Escape')
        assert not page.locator('#modal').evaluate('(el) => el.open')
        checks.append('Native dialog keyboard confinement and Escape')

        page.locator('#scenario').select_option('synthesis')
        page.locator('.tabs [data-page="brief"]').click()
        page.locator('[data-action="toggle-context"]').click()
        assert page.locator('#context').is_hidden()
        page.locator('.judgment [data-source="A"]').click()
        assert page.locator('#context').is_visible()
        page.keyboard.press('Escape')
        page.locator('#context [data-action="defer"]').click()
        page.locator('.global-nav [data-action="decisions"]').click()
        assert page.get_by_role('heading', name='当前没有新的待批准事项').is_visible()
        assert page.get_by_role('dialog').get_by_text('等预算先导实验已暂缓。草案仍保留，不会自动执行。').is_visible()
        page.keyboard.press('Escape')
        checks.append('Context toggle and deferred-inbox consistency')
        # State gallery on desktop and mobile.
        for width in [390, 1440]:
            page.set_viewport_size({'width': width, 'height': 1000})
            page.locator('.tabs [data-page="brief"]').click()
            for scenario in ['initial', 'approval', 'disconnected', 'interrupted', 'stale', 'complete', 'synthesis']:
                page.locator('#scenario').select_option(scenario)
                assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1'), (width, scenario)
                if width == 1440 and scenario in ['initial', 'interrupted', 'stale']:
                    page.screenshot(path=str(output / f'05-state-{scenario}.png'), full_page=True)
            checks.append(f'7 scientific/execution states at {width}px')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('.tabs [data-page="brief"]').click()
        page.screenshot(path=str(output / '06-brief-mobile.png'), full_page=True)
        page.locator('.judgment [data-source="A"]').click()
        assert page.locator('#modal').evaluate('(el) => el.open')
        assert page.get_by_role('dialog').get_by_text('示例文献 · 非真实论文').is_visible()

        page.get_by_role('dialog').get_by_role('button', name='打开原文位置').click()
        assert page.locator('#modal-title').evaluate('(el) => document.activeElement === el')
        page.screenshot(path=str(output / '07-source-mobile.png'), full_page=True)
        checks.append('Nested source preview announces and focuses its new heading')
        page.keyboard.press('Escape')
        checks.append('Mobile evidence opens an accessible modal instead of cramped side panel')
        # Empty input feedback and escaped user-supplied question.
        page.locator('.breadcrumb [data-page="home"]').click()
        page.get_by_role('button', name='开始梳理问题 →').click()
        assert page.locator('#new-question').get_attribute('aria-invalid') == 'true'
        page.locator('#new-question').fill('<img src=x onerror="alert(1)">如何定义严格的等预算评测？')
        page.get_by_role('button', name='开始梳理问题 →').click()
        assert page.locator('main img').count() == 0
        assert page.get_by_role('heading', name='先把问题定义清楚。').is_visible()

        page.locator('.tabs [data-page="experiments"]').click()
        assert page.get_by_role('heading', name='先准备依据，再形成判断。').is_visible()
        assert page.locator('main').get_by_text('EXP-01 / 最小区分实验').count() == 0
        page.locator('.topbar [data-action="commands"]').click()
        page.locator('[data-command-page="brief"]').click()
        page.locator('#scenario').select_option('synthesis')
        page.set_viewport_size({'width':1440,'height':1100})
        page.locator('.breadcrumb [data-page="home"]').click()
        page.screenshot(path=str(output / '08-home-desktop.png'), full_page=True)
        page.locator('.tabs [data-page="hypotheses"]').click()
        page.screenshot(path=str(output / '09-hypotheses.png'), full_page=True)
        page.locator('.tabs [data-page="outputs"]').click()
        page.screenshot(path=str(output / '10-outputs.png'), full_page=True)
        checks.append('New question has no inherited experiment, evidence, or approval')
        checks.append('Inline validation and escaped question input')
        assert not errors, errors
        assert not requests, requests
        report = {'status': 'passed', 'checks': checks, 'page_errors': errors, 'network_requests': requests, 'screenshots': sorted(p.name for p in output.glob('*.png')), 'scope': 'Synthetic local prototype only; no production or research-quality validation.'}
        (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        context.close()
        browser.close()


if __name__ == '__main__':
    run()
