#!/usr/bin/env python3
"""Render and exercise the standalone conversation prototype. No backend calls.

Serves only repository assets on ephemeral loopback, blocks external/API requests,
uses actual Chromium and the actual UI factories, captures three themes/widths.
Screenshots certify this synthetic prototype, not production or paid execution.
"""
from io import BytesIO as BytesIO
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

import re
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen
from urllib.error import HTTPError
import subprocess
import sys
from threading import Thread

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from chromium_env import ensure_chromium_env, chromium_executable
from playwright.sync_api import sync_playwright


def build():

    subprocess.run(['node', str(HERE / 'check-style.mjs')], cwd=ROOT, check=True)
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
                    str(ROOT / 'frontend/src/ui/index.ts'), '--bundle', '--format=esm',
                    f'--outfile={HERE / "ui-kit.bundle.js"}'], cwd=ROOT, check=True)
    # Bounded preview-only subset of an installed CJK font. No font downloads,
    # no production font changes, and no missing glyphs on this rootless host.
    from fontTools.ttLib import TTFont
    from fontTools.subset import Subsetter
    source = Path(sys.base_prefix) / 'fonts/NotoSansCJKsc-VF.ttf'
    if source.is_file():
        font = TTFont(source)
        subset = Subsetter()
        subset.populate(text=(HERE / 'index.html').read_text() +
                        (HERE / 'prototype.js').read_text() + '关闭面板取消')
        subset.subset(font)
        font.flavor = 'woff2'
        font.save(HERE / 'preview-cjk.woff2')

    subprocess.run([sys.executable, str(HERE / 'build-preview.py')], cwd=ROOT, check=True)


ASSET_PATHS = {'/designs/sidebar-coordinator/' + filename for filename in (
    'index.html', 'prototype.css', 'prototype.js', 'ui-kit.bundle.js', 'preview-cjk.woff2')}
ASSET_PATHS.update(('/frontend/src/styles/fonts.css', '/static/styles.css'))
for font in re.findall(r'url\(([^)]+)\)', (ROOT / 'frontend/src/styles/fonts.css').read_text()):
    asset = (ROOT / 'frontend/src/styles' / font.strip('\"\'')).resolve()
    ASSET_PATHS.add('/' + asset.relative_to(ROOT).as_posix())


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if unquote(urlsplit(self.path).path) not in ASSET_PATHS:
            self.send_error(403, 'Not a public preview asset')
            return
        super().do_GET()

    def do_HEAD(self):
        self.send_error(405, 'Preview accepts GET only')


def main():
    build()
    output = HERE / 'previews'
    output.mkdir(exist_ok=True)
    ensure_chromium_env()
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT)))
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    results, errors, requests_denied, failed_requests = [], [], [], []
    report = {'scope': 'Chromium; standalone synthetic prototype; NO backend, provider, or production-state mutation', 'cases': results}

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        results.append(label)
        print('[PASS]', label, flush=True)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=chromium_executable(),
                args=['--no-sandbox', '--disable-dev-shm-usage'])

            def new_page(width=1440, height=1000, theme='tofu'):
                page = browser.new_page(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                page.on('requestfailed', lambda request: failed_requests.append(request.url))
                page.on('response', lambda response: errors.append(f'HTTP {response.status}: {response.url}') if response.status >= 400 else None)

                def route(request):
                    url = request.request.url
                    if url.startswith(origin + '/') and unquote(urlsplit(url).path) in ASSET_PATHS and request.request.method == 'GET':
                        request.continue_()
                    else:
                        requests_denied.append(url)
                        request.abort()
                page.route('**/*', route)
                page.goto(f'{origin}/designs/sidebar-coordinator/index.html?theme={theme}', wait_until='networkidle')
                page.wait_for_function('window.__prototypeReady === true')
                page.evaluate('document.fonts.ready')
                return page

            def close_modal(page):
                page.keyboard.press('Escape')
                page.wait_for_function('!document.querySelector("dialog")')

            def modal_geometry(page, label):
                dialog = page.get_by_role('dialog')
                dialog.wait_for(state='visible')

                name = dialog.get_attribute('aria-label')
                check(label + ': modal has an accessible name', bool(name) and page.get_by_role('dialog', name=name, exact=True).count() == 1)
                page.wait_for_function('getComputedStyle(document.querySelector("dialog")).transform === "none"')
                geometry = dialog.evaluate('d => {const r=d.getBoundingClientRect();return {left:r.left,right:r.right,scroll:d.scrollWidth,width:d.clientWidth}}')
                check(label + ': modal remains within viewport ' + str(geometry), geometry['left'] >= -1 and geometry['right'] <= page.viewport_size['width'] + 1 and geometry['scroll'] <= geometry['width'] + 1)

            def scenario(page, value):
                if page.viewport_size['width'] <= 720:
                    page.get_by_role('button', name='打开对话列表', exact=True).click()
                page.locator('#design-controls').click()
                page.get_by_label('演示场景').select_option(value)
                page.locator('#apply-fixture').click()
                page.wait_for_function('!document.querySelector("dialog")')

                if page.viewport_size['width'] <= 720:
                    check('mobile scene change returns focus to visible main content', page.evaluate('document.getElementById("main").contains(document.activeElement) && document.activeElement.getClientRects().length > 0'))

            def accept_research(page):
                page.locator('#task-primary').click()
                page.get_by_role('button', name='接受方案 B', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')

            def authorize(page):
                page.locator('#task-primary').click()
                page.get_by_label('我已确认对象、范围及有限预算（仅演示）', exact=True).check()
                page.get_by_role('button', name='确认授权 · 演示', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')

            for theme in ('tofu', 'dark', 'light'):
                for width, height, label in ((1440, 1000, 'desktop'), (390, 844, 'mobile'), (320, 800, 'small')):
                    page = new_page(width, height, theme)
                    prefix = f'{theme}/{width}'
                    check(prefix + ': no page horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                    check(prefix + ': no thread horizontal overflow', page.locator('#thread-scroll').evaluate('e=>e.scrollWidth<=e.clientWidth'))

                    check(prefix + ': prose stays inside reading column', page.locator('.assistant-prose').evaluate('e=>e.scrollWidth<=e.clientWidth'))
                    if width == 1440:
                        page.locator('#task-research').hover()
                        check(prefix + ': task hover links sidebar worker', page.locator('#nav-research').locator('..').locator('..').get_attribute('data-linked') == 'true')
                        page.locator('#header-evidence').hover()
                        page.locator('#conversation-search').fill('兼容')
                        check(prefix + ': search filters workers without hiding Coordinator', page.locator('#nav-compat').count() == 1 and page.locator('#nav-research').count() == 0 and page.locator('#nav-coordinator').count() == 1)
                        page.locator('#conversation-search').fill('')
                    page.evaluate('document.activeElement?.blur()')
                    page.mouse.move(0, 0)
                    page.screenshot(path=str(output / f'{label}-{theme}.png'))
                    # A second capture documents scrolling to the next action,
                    # without presenting that scrolled state as the initial view.
                    page.locator('#task-primary').scroll_into_view_if_needed()
                    page.screenshot(path=str(output / f'{label}-action-{theme}.png'))
                    page.locator('#task-primary').focus()
                    page.keyboard.press('Enter')
                    modal_geometry(page, prefix)
                    for key in ('Tab', 'Shift+Tab') * 6:
                        page.keyboard.press(key)
                        check(prefix + ': ' + key + ' is trapped in modal', page.evaluate('document.querySelector("dialog").contains(document.activeElement)'))
                    if width == 1440:
                        page.screenshot(path=str(output / f'review-{theme}.png'))
                    close_modal(page)
                    check(prefix + ': Escape restores exact opening focus', page.locator('#task-primary').evaluate('e=>e===document.activeElement'))
                    check(prefix + ': dismissed review does not authorize', '等待方案审阅' in page.locator('#thread').inner_text())

                    if width <= 720:
                        page.get_by_role('button', name='打开对话列表', exact=True).click()
                        check(prefix + ': mobile list disables background', page.locator('#main').evaluate('e=>e.inert'))
                        modal_list_width = page.locator('#sidebar').evaluate('e=>e.scrollWidth<=e.clientWidth')
                        check(prefix + ': mobile list does not overflow', modal_list_width)
                        page.screenshot(path=str(output / f'list-{label}-{theme}.png'))
                        page.locator('#nav-research').click()
                        check(prefix + ': worker navigation restores main interaction', not page.locator('#main').evaluate('e=>e.inert'))
                    else:
                        page.locator('#nav-research').click()
                    check(prefix + ': worker opens ordinary chat', page.locator('#conversation-title').inner_text() == '登录方案研究')

                    check(prefix + ': navigation focuses the conversation heading', page.locator('#conversation-title').evaluate('e=>e===document.activeElement'))

                    if theme == 'tofu' and width in (1440, 390):
                        page.screenshot(path=str(output / f'worker-{label}-{theme}.png'))
                    page.locator('#back-coordinator').click()
                    check(prefix + ': return preserves task', page.locator('#conversation-title').inner_text() == 'Coordinator')
                    page.locator('#message-input').fill('保持草稿')
                    page.locator('#task-research').click()
                    page.locator('#back-coordinator').click()
                    check(prefix + ': draft is scoped and survives navigation', page.locator('#message-input').input_value() == '保持草稿')
                    page.locator('#message-input').fill('<img src=x onerror=alert(1)>')
                    page.locator('#send-message').click()
                    check(prefix + ': user text is not executable markup', page.locator('#thread img').count() == 0 and '<img src=x' in page.locator('#thread').inner_text())
                    page.locator('#message-input').fill('第一行')
                    page.locator('#message-input').press('Shift+Enter')
                    page.locator('#message-input').type('第二行')
                    check(prefix + ': Shift+Enter inserts newline', '\n' in page.locator('#message-input').input_value())
                    page.close()

            for asset in ('/docs/README.md', '/AGENTS.md'):
                denied = False
                try:
                    with urlopen(origin + asset) as response:
                        response.read(1)
                except HTTPError as error:
                    denied = error.code == 403
                check('static server refuses non-public source: ' + asset, denied)

            resize_page = new_page()
            resize_page.locator('#nav-research').focus()
            resize_page.set_viewport_size({'width': 320, 'height': 800})
            resize_page.wait_for_function('document.getElementById("main").contains(document.activeElement) && document.activeElement !== document.body')
            check('desktop-to-mobile resize retains visible main focus', resize_page.evaluate('document.activeElement.getClientRects().length > 0'))
            check('resized header stays within narrow viewport', resize_page.locator('.conversation-header').evaluate('e=>e.scrollWidth<=e.clientWidth'))
            resize_page.close()

            # Complete normal journey, with separate execution/review/delivery evidence.
            page = new_page()
            accept_research(page)
            check('accepting research leaves execution unstarted', '等待授权' in page.locator('#thread').inner_text())
            page.locator('#task-primary').click()
            modal_geometry(page, 'authorization')
            confirm = page.get_by_role('button', name='确认授权 · 演示', exact=True)
            check('authorization requires consent', confirm.is_disabled())
            checkbox = page.get_by_label('我已确认对象、范围及有限预算（仅演示）', exact=True)
            checkbox.check()
            check('consent enables exact-scope authorization', confirm.is_enabled())
            page.locator('#grant-path').fill('src/other/')
            check('editing scope clears consent', not checkbox.is_checked() and confirm.is_disabled())
            page.locator('#grant-path').fill('src/auth/')
            checkbox.check()
            page.screenshot(path=str(output / 'authorization-tofu.png'))
            confirm.click()
            page.wait_for_function('!document.querySelector("dialog")')
            check('explicit authorization changes only mock execution', '进行中' in page.locator('#thread').inner_text())
            page.locator('#design-controls').click()
            page.locator('#demo-complete').click()
            page.wait_for_function('!document.querySelector("dialog")')
            check('execution completion is not review or delivery', '执行完成 · 待审阅' in page.locator('#thread').inner_text() and '等待文件交付' in page.locator('#thread').inner_text())
            page.locator('#task-primary').click()
            page.get_by_role('button', name='接受实现结果', exact=True).click()
            page.wait_for_function('!document.querySelector("dialog")')
            check('accepted implementation is not delivered', '交付未确认' in page.locator('#thread').inner_text())
            page.locator('#task-primary').click()
            page.get_by_role('button', name='提交交付请求 · 演示', exact=True).click()
            page.wait_for_function('!document.querySelector("dialog")')
            check('publish request is not a delivery receipt', '交付回执未知' in page.locator('#thread').inner_text() and '等待文件交付' in page.locator('#thread').inner_text())
            page.locator('#task-primary').click()
            page.get_by_role('button', name='模拟准确交付回执到达', exact=True).click()
            page.wait_for_function('!document.querySelector("dialog")')
            check('exact receipt satisfies dependency but does not dispatch', '依赖已满足 · 未授权' in page.locator('#thread').inner_text())
            page.locator('#task-primary').scroll_into_view_if_needed()
            page.screenshot(path=str(output / 'delivered-tofu.png'))
            page.close()

            for width in (1440, 320):
                page = new_page(width, 1000 if width == 1440 else 800)
                scenario(page, 'uncertain')
                accept_research(page)
                authorize(page)
                check(f'{width}: uncertain grant stays unknown', '授权回执未知' in page.locator('#thread').inner_text())
                page.locator('#task-primary').click()
                modal_geometry(page, f'{width}: unknown receipt')
                close_modal(page)
                check(f'{width}: closing unknown receipt does not reset request', '授权回执未知' in page.locator('#thread').inner_text())
                page.locator('#task-primary').click()
                page.get_by_role('button', name='模拟收到原请求回执', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')
                check(f'{width}: original request reconciliation shows running', '进行中' in page.locator('#thread').inner_text())
                page.locator('#task-frontend').click()
                page.locator('#worker-withdraw').click()
                page.get_by_role('button', name='撤销本次授权 · 演示', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')
                check(f'{width}: withdrawal does not invent cancellation ACK', '授权已撤销 · 取消待确认' in page.locator('#thread').inner_text())

                check(f'{width}: withdrawal retains a meaningful keyboard focus', page.locator('#conversation-title').evaluate('e=>e===document.activeElement'))
                page.locator('#back-coordinator').click()
                scenario(page, 'unbound')
                check(f'{width}: unbound does not auto-promote worker', '协调角色未设置' in page.locator('#thread').inner_text())
                page.locator('#task-primary').click()
                page.get_by_role('button', name='绑定示例 Coordinator', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')
                check(f'{width}: binding does not authorize work', '等待方案审阅' in page.locator('#thread').inner_text())
                scenario(page, 'blocked')
                accept_research(page)
                authorize(page)
                if width == 320:
                    page.get_by_role('button', name='打开对话列表', exact=True).click()
                page.locator('#design-controls').click()
                page.locator('#demo-complete').click()
                page.wait_for_function('!document.querySelector("dialog")')

                if width == 320:
                    check('mobile result arrival returns focus to visible main content', page.evaluate('document.getElementById("main").contains(document.activeElement) && document.activeElement.getClientRects().length > 0'))
                page.locator('#task-primary').click()
                page.get_by_role('button', name='接受实现结果', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')
                page.locator('#task-primary').click()
                page.get_by_role('button', name='提交交付请求 · 演示', exact=True).click()
                page.wait_for_function('!document.querySelector("dialog")')
                page.locator('#header-evidence').click()
                check(f'{width}: failed publication keeps downstream waiting', '门禁拒绝' in page.get_by_role('dialog').inner_text())
                modal_geometry(page, f'{width}: evidence and full SHA')
                page.screenshot(path=str(output / f'blocked-{width}-tofu.png'))
                close_modal(page)
                page.close()

            page = new_page()
            page.locator('#new-chat').click()
            check('new conversation creates an independent local chat', page.locator('#conversation-title').inner_text() == '新对话 1')
            page.locator('#message-input').fill('只在新对话里')
            page.locator('#send-message').click()
            page.locator('#nav-coordinator').click()
            check('new local chat does not leak messages into Coordinator', '只在新对话里' not in page.locator('#thread').inner_text())
            page.close()

            # Open the actual offline file, not an HTTP simulation of it.
            offline = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            offline.on('pageerror', lambda error: errors.append(str(error)))
            attempted = []
            artifact_url = (HERE / 'preview.html').as_uri()
            def offline_route(route):
                if route.request.url == artifact_url:
                    route.continue_()
                else:
                    attempted.append(route.request.url)
                    route.abort()
            offline.route('**/*', offline_route)
            offline.goto(artifact_url, wait_until='networkidle')
            offline.wait_for_function('window.__prototypeReady === true')
            offline.evaluate('document.fonts.ready')
            check('offline artifact opens from file without external assets', not attempted)
            accept_research(offline)
            check('offline artifact retains actual interactive review', '等待授权' in offline.locator('#thread').inner_text())
            offline.locator('#task-primary').click()
            modal_geometry(offline, 'offline authorization')
            close_modal(offline)
            offline.screenshot(path=str(output / 'offline-preview-tofu.png'))
            offline.close()

            check('zero uncaught errors, console errors or failed HTTP responses', not errors)
            check('zero external, backend or non-GET requests', not requests_denied)
            check('zero failed asset requests', not failed_requests)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        report.update({'errors': errors, 'denied_requests': requests_denied, 'failed_requests': failed_requests, 'check_count': len(results)})
        (output / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(results)} checks passed; screenshots -> {output}', flush=True)


def offline_check():
    """Narrow ladder for a changed standalone generator only."""
    build()
    ensure_chromium_env()
    errors, attempts = [], []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium_executable(), args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
            url = (HERE / 'preview.html').as_uri()
            def route(request):
                if request.request.url == url:
                    request.continue_()
                else:
                    attempts.append(request.request.url)
                    request.abort()
            page.route('**/*', route)
            page.goto(url, wait_until='networkidle')
            page.wait_for_function('window.__prototypeReady === true')
            page.evaluate('document.fonts.ready')
            page.locator('#task-primary').click()
            panel = page.get_by_role('dialog', name='审阅登录方案 · R1', exact=True)
            assert panel.is_visible()
            page.get_by_role('button', name='接受方案 B', exact=True).click()
            page.wait_for_function('!document.querySelector("dialog")')
            assert '等待授权' in page.locator('#thread').inner_text()
            page.locator('#task-primary').click()
            assert page.get_by_role('dialog', name='确认本次执行范围', exact=True).is_visible()
            assert page.get_by_role('button', name='确认授权 · 演示', exact=True).is_disabled()
            page.keyboard.press('Escape')
            page.wait_for_function('!document.querySelector("dialog")')
            assert page.locator('#task-primary').evaluate('e=>e===document.activeElement')
            assert not errors and not attempts, (errors, attempts)
            result = {'scope': 'offline artifact narrow check; synthetic only', 'named_modals': True,
                      'review_does_not_execute': True, 'consent_required': True,
                      'focus_restored': True, 'errors': errors, 'external_attempts': attempts}
            (HERE / 'previews/offline-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print('Offline generator narrow browser checks passed.')
        finally:
            browser.close()


if __name__ == '__main__':
    if '--offline-only' in sys.argv:
        offline_check()
    else:
        main()
