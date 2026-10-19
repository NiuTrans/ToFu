"""Exercise the real dispatch drawer in Chromium with explicit static mocks only.

npx vite build --config designs/project-coordination/dispatch-preview.mjs
python3 designs/project-coordination/dispatch_capture.py
NOT backend/native acceptance. No provider, credentials or external requests.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
from threading import Thread

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from chromium_env import ensure_chromium_env
from capture import evidence_font


def fill(page, files=False):
    for name, value in {'turnId': 'mock-human-turn', 'recipientId': 'mock-worker',
                        'brief': '核验范围内任务；不自动发布', 'model': 'mock.not-callable',
                        'maxTokens': '100', 'maxModelRequests': '1', 'timeout': '60'}.items():
        field = page.locator(f'[name="{name}"]')
        if name in ('turnId', 'recipientId'):
            field.select_option(value)
        else:
            field.fill(value)
    page.locator('[name="mode"]').select_option('files' if files else 'consult')
    if files:
        page.locator('[name="readPaths"]').fill('src')
        page.locator('[name="writePaths"]').fill('src')
        page.locator('[name="maxToolCalls"]').fill('8')
        page.locator('[name="edit_file"]').check()


def main():
    ensure_chromium_env()
    font = evidence_font()
    output = Path(__file__).parent / 'previews' / 'dispatch'
    output.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
        SimpleHTTPRequestHandler, directory=str(ROOT / '.native/project-dispatch-preview')))
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    report = {'scope': 'True Chromium; static UI mock ports ONLY; NOT backend/native acceptance; no paid models', 'cases': []}
    try:
        with sync_playwright() as runner:
            browser = runner.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage'])
            try:
                for width in (1440, 390, 320):
                    for theme in ('tofu', 'dark', 'light'):
                        page = browser.new_page(viewport={'width': width, 'height': 1000})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.route('**/*', lambda route: route.continue_()
                                   if route.request.url.startswith(origin + '/') else route.abort())
                        if font:
                            page.route('**/coordination-evidence.ttf', lambda route: route.fulfill(
                                body=font, content_type='font/ttf'))
                        page.goto(origin + '/designs/project-coordination/dispatch-preview.html')
                        page.wait_for_function('window.previewReady === true')
                        page.evaluate('(value)=>document.documentElement.dataset.theme=value', theme)
                        if font:
                            page.add_style_tag(content='@font-face{font-family:CoordinationEvidence;src:url("/coordination-evidence.ttf")} :root{--font-ui:CoordinationEvidence,sans-serif}')
                            page.evaluate('document.fonts.ready')
                        trigger = page.get_by_role('button', name='打开当前协调抽屉', exact=True)

                        def open_drawer():
                            trigger.focus(); trigger.press('Enter')
                            page.get_by_text('授权项目任务', exact=True).click()
                            page.wait_for_function("document.querySelector('[data-testid=project-dispatch] article') !== null")

                        def close_drawer():
                            page.keyboard.press('Escape')
                            page.wait_for_function('!document.querySelector("dialog")')
                            assert trigger.evaluate('(node)=>node===document.activeElement')

                        def calls():
                            return page.evaluate('window.mockCalls')

                        def card(assignment):
                            return page.locator('[data-testid=project-dispatch] article').filter(
                                has=page.get_by_text(assignment, exact=True))


                        def assert_reference_ranges(locator):
                            bounds = locator.evaluate_all('''nodes => {
                                const dialog = document.querySelector('dialog').getBoundingClientRect();
                                const rects = [];
                                for (const node of nodes) {
                                    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
                                    while (walker.nextNode()) {
                                        const range = document.createRange(); range.selectNodeContents(walker.currentNode);
                                        for (const rect of range.getClientRects()) rects.push({left: rect.left, right: rect.right});
                                    }
                                }
                                return {left: dialog.left, right: dialog.right, rects};
                            }''')
                            assert all(bounds['left'] <= rect['left'] and rect['right'] <= bounds['right']
                                       for rect in bounds['rects']), bounds

                        open_drawer()
                        dialog = page.get_by_role('dialog')
                        assert calls() == []
                        assert '请核验 src/task.ts 的范围与预算' in page.locator('[name="turnId"]').inner_text()
                        assert page.locator('[name="mode"]').input_value() == 'consult'
                        assert not page.locator('[name="readPaths"]').is_visible()
                        fill(page)
                        submit = page.get_by_role('button', name='授权并派发任务', exact=True)
                        assert submit.is_disabled()
                        dependency_draft = page.locator('[data-testid=dispatch-dependencies]')
                        def pin(assignment):
                            return dependency_draft.locator(f'[data-assignment-id="{assignment}"]')
                        dependency_pins = [
                            {'assignmentId': 'mock-upstream-consult', 'grantRevision': 2, 'resultRevision': 11},
                            {'assignmentId': 'mock-upstream-files', 'grantRevision': 2, 'resultRevision': 11,
                             'checkpointSha': '0123456789abcdef0123456789abcdef01234567'},
                        ]
                        page.locator('[name="consent"]').check()
                        pin('mock-upstream-consult').check()
                        assert not page.locator('[name="consent"]').is_checked()
                        pin('mock-upstream-files').check()
                        assert calls() == []
                        dependency_draft.scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-dependencies-selected.png'))

                        reference_geometry = dependency_draft.locator('.ui-choice > span').evaluate_all(
                            '(nodes)=>nodes.map(node=>({client:node.clientWidth,scroll:node.scrollWidth}))')
                        assert all(item['scroll'] <= item['client'] + 1 for item in reference_geometry), reference_geometry

                        assert_reference_ranges(dependency_draft.locator('.ui-choice > span'))
                        geometry = dialog.evaluate('(node)=>({client:node.clientWidth,scroll:node.scrollWidth})')
                        assert geometry['scroll'] <= geometry['client'] + 1, geometry
                        page.locator('summary').scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-consult.png'))
                        page.locator('[name="consent"]').check()
                        submit.click()
                        page.wait_for_function("window.mockCalls.length === 1 && window.mockCalls[0].name === 'grant'")
                        assert 'profile' not in calls()[0]['body']

                        assert calls()[0]['body']['dependencies'] == dependency_pins
                        page.wait_for_function('document.querySelector("[name=mode]").disabled === false')
                        assert not page.locator('[name="consent"]').is_checked()

                        waiting = card('mock-new-mock-command-1').locator('[data-testid=dependency-states]')
                        waiting.get_by_text('等待审阅 · 当前结果须接受审阅', exact=True).wait_for()
                        waiting.get_by_text('等待交付 · 仍须正式交付', exact=True).wait_for()
                        waiting.scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-dependencies-waiting.png'))

                        assert_reference_ranges(waiting.locator('small'))

                        # Explicit file opt-in, invalid scope fence and fresh consent.
                        fill(page, files=True)
                        assert not page.locator('[name="write_file"]').is_checked()

                        assert page.locator('[name="read_files"]').is_checked()
                        page.locator('[name="readPaths"]').fill('../src')
                        page.locator('[name="consent"]').check(); submit.click()
                        page.get_by_text('文件任务须填写相对读取路径及明确的 1–512 次工具预算。写入或编辑须明确指定读取范围内的写入路径。禁止运行时元数据、别名与路径跳转。', exact=True).wait_for()
                        assert len(calls()) == 1
                        page.locator('[name="readPaths"]').fill('src')

                        assert page.get_by_text('文件任务须填写相对读取路径及明确的 1–512 次工具预算。写入或编辑须明确指定读取范围内的写入路径。禁止运行时元数据、别名与路径跳转。', exact=True).count() == 0
                        assert not page.locator('[name="consent"]').is_checked()
                        page.locator('[name="consent"]').check()
                        page.locator('[name="consent"]').scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-files.png'))
                        pin('mock-upstream-consult').check(); pin('mock-upstream-files').check()
                        assert not page.locator('[name="consent"]').is_checked()
                        page.locator('[name="consent"]').check()
                        page.evaluate('window.mockLoseNextGrant = true')
                        submit.click()
                        page.wait_for_function('window.mockCalls.length === 2')
                        page.get_by_role('button', name='原样重试此授权', exact=True).wait_for()
                        close_drawer(); open_drawer()
                        assert page.locator('[name="mode"]').input_value() == 'files'
                        assert page.locator('[name="maxToolCalls"]').is_disabled()

                        assert pin('mock-upstream-files').is_checked() and pin('mock-upstream-files').is_disabled()
                        page.get_by_role('button', name='原样重试此授权', exact=True).click()
                        page.wait_for_function('window.mockCalls.length === 3')
                        assert calls()[1]['body'] == calls()[2]['body']
                        assert calls()[1]['key'] == calls()[2]['key']

                        assert calls()[2]['body']['dependencies'] == dependency_pins
                        assert calls()[2]['body']['profile'] == {'mode': 'files', 'tools': ['read_files', 'edit_file'],
                            'readPaths': ['src'], 'writePaths': ['src'], 'maxToolCalls': 8}
                        page.wait_for_function('document.querySelector("[name=mode]").disabled === false')

                        # Acceptance and publication are distinct actions and neither is delivery.
                        accepted = card('mock-completed')
                        assert accepted.get_by_role('button', name='明确发布已接受的文件结果', exact=True).count() == 0
                        accepted.get_by_role('button', name='接受审阅结果 · 不等于发布', exact=True).click()
                        page.wait_for_function('window.mockCalls.length === 4')
                        assert calls()[3]['name'] == 'review'
                        publish = accepted.get_by_role('button', name='明确发布已接受的文件结果', exact=True)
                        publish.wait_for(); publish.click()
                        page.wait_for_function('window.mockCalls.length === 5')
                        assert calls()[4] == {'name': 'publish', 'body': {'projectPath': '/mock/project',
                            'assignmentId': 'mock-completed', 'expectedRevision': 2, 'resultRevision': 11,
                            'expectedCheckpointSha': 'mock-checkpoint'}}
                        page.get_by_text('交付原因：mock_gate_held', exact=True).wait_for()
                        assert '正式交付：否' in accepted.inner_text()
                        assert 'mock-checkpoint' in accepted.inner_text()
                        accepted.scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-held-publication.png'))

                        # Rework review does not grant execution; preparing draft never POSTs.
                        rework = card('mock-rework')
                        rework.get_by_role('button', name='要求返工 · 再次执行须重新授权并设置预算', exact=True).click()
                        page.wait_for_function('window.mockCalls.length === 6')
                        draft = rework.get_by_role('button', name='准备返工新授权 · 仅草稿', exact=True)
                        draft.wait_for(); pin('mock-upstream-files').check(); draft.click()
                        assert not pin('mock-upstream-files').is_checked()
                        assert len(calls()) == 6
                        for name in ('turnId', 'projectionRevision', 'model', 'maxTokens', 'maxModelRequests', 'timeout', 'maxToolCalls'):
                            assert page.locator(f'[name="{name}"]').input_value() == '', name
                        assert not page.locator('[name="consent"]').is_checked()
                        assert submit.is_disabled()
                        assert '授权版本 2 · 结果版本 11' in dialog.inner_text()
                        page.locator('summary').scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-fresh-rework.png'))
                        fill(page, files=True); page.locator('[name="consent"]').check(); submit.click()
                        page.wait_for_function('window.mockCalls.length === 7')
                        assert calls()[6]['body']['reworkOf'] == {'assignmentId': 'mock-rework', 'grantRevision': 2, 'resultRevision': 11}
                        assert calls()[6]['key'] != calls()[2]['key']

                        assert 'dependencies' not in calls()[6]['body']
                        page.wait_for_function('document.querySelector("[name=mode]").disabled === false')
                        # Read-only refresh preserves local pins through changed/disappeared candidates.
                        pin('mock-upstream-consult').check(); pin('mock-upstream-files').check()
                        page.evaluate('window.mockAdvanceDependencies()')
                        page.get_by_role('button', name='刷新咨询回执', exact=True).click()
                        dependency_draft.get_by_text('依赖版本已过期或尚未确认', exact=True).first.wait_for()
                        assert pin('mock-upstream-files').is_checked() and pin('mock-upstream-consult').is_checked()
                        assert '0123456789abcdef0123456789abcdef01234567' in dependency_draft.inner_text()
                        assert 'abcdef0123456789abcdef0123456789abcdef0123' not in dependency_draft.inner_text()
                        assert len(calls()) == 7
                        dependency_draft.scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-dependencies-stale-draft.png'))
                        waiting.get_by_text('无效 / 已过期 · 检查点已变化', exact=True).wait_for()
                        waiting.get_by_text('无效 / 已过期 · 结果版本已变化', exact=True).wait_for()
                        waiting.scroll_into_view_if_needed()
                        page.screenshot(path=str(output / f'{theme}-{width}-dependencies-stale-server.png'))

                        assert_reference_ranges(waiting.locator('small'))
                        assert_reference_ranges(dependency_draft.locator('.ui-choice > span'))
                        final_geometry = dialog.evaluate('(node)=>({client:node.clientWidth,scroll:node.scrollWidth})')
                        assert final_geometry['scroll'] <= final_geometry['client'] + 1, final_geometry
                        close_drawer()
                        assert not errors, errors
                        report['cases'].append({'width': width, 'theme': theme, 'noAutoGrant': True,
                            'freshConsent': True, 'scopeFence': True, 'exactUncertainRetryAfterReopen': True,

                            'explicitDependencyPins': dependency_pins, 'dependencySelectionNoPost': True,
                            'dependencyConsentReset': True, 'backendMockWaitingToStale': True,
                            'stalePinsPreserved': True, 'dependencyRetryFrozen': True,
                            'reviewNotPublish': True, 'reworkDraftNoPost': True, 'freshReworkLineage': True,
                            'canonicalDelivery': False, 'mockCalls': calls(), 'geometry': geometry,
                            'finalGeometry': final_geometry, 'focusRestored': True, 'pageErrors': errors})

                        page.close()
            finally:
                browser.close()
    finally:
        server.shutdown(); server.server_close()
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'scope': report['scope'], 'cases': len(report['cases']), 'passed': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
