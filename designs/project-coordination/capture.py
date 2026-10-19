"""Capture the three navigation alternatives and exercise the actual drawer.

Build with npx vite build --config designs/project-coordination/vite.preview.mjs,
then python3 designs/project-coordination/capture.py. Fixtures are
explicit demo data; this is rendering evidence, not paid-agent execution proof.
"""
import json
import sys
from pathlib import Path

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial
from threading import Thread
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from chromium_env import ensure_chromium_env


def evidence_font():
    # Static, bounded fixture subset from an already installed font. This is
    # screenshot infrastructure, not a new application font/download.
    from io import BytesIO
    from fontTools.ttLib import TTFont
    from fontTools.subset import Subsetter
    from fontTools.varLib.instancer import instantiateVariableFont
    source = Path(sys.base_prefix) / 'fonts/NotoSansCJKsc-VF.ttf'
    if not source.is_file():
        return None
    font = TTFont(source)
    locale = json.loads((ROOT / 'frontend/src/i18n/locales/zh.json').read_text())
    subset = Subsetter()
    subset.populate(text=(Path(__file__).parent / 'preview.html').read_text() +
                    (Path(__file__).parent / 'dispatch-preview.html').read_text() +
                    ''.join(value for key, value in locale.items()
                            if key.startswith(('projectCoordination.', 'projectDispatch.'))) + '关闭面板')
    subset.subset(font)
    font = instantiateVariableFont(font, {'wght': 400}, inplace=True)
    output = BytesIO()
    font.save(output)
    return output.getvalue()


def main():
    ensure_chromium_env()

    font_bytes = evidence_font()
    output = Path(__file__).resolve().parent / 'previews'
    output.mkdir(exist_ok=True)
    results = []

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(SimpleHTTPRequestHandler, directory=str(ROOT / '.native/project-coordination-preview')))
    Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=['--no-sandbox', '--disable-gpu'])
        try:
            for theme in ('tofu', 'dark', 'light'):
                for width in (1440, 390, 320):
                    for mode in ('fixed', 'switch', 'drawer') if width == 1440 else ('drawer',):
                        page = browser.new_page(viewport={'width': width, 'height': 1000})
                        errors = []

                        if font_bytes:
                            page.route('**/coordination-evidence.ttf', lambda route: route.fulfill(body=font_bytes, content_type='font/ttf'))
                        page.on('pageerror', lambda e: errors.append(str(e)))
                        page.goto(f'{origin}/designs/project-coordination/preview.html?theme={theme}&mode={mode}&childMessages=1', wait_until='networkidle')
                        page.wait_for_function('window.previewReady === true')

                        # This rootless host resolves generic zh sans-serif to DejaVu.
                        # Use the installed CJK face for evidence, without changing app fonts.
                        if font_bytes:
                            page.add_style_tag(content='@font-face{font-family:CoordinationEvidence;src:url("/coordination-evidence.ttf")} :root{--font-ui:CoordinationEvidence,sans-serif} body{font-family:var(--font-ui)}')
                        page.evaluate('document.fonts.ready')
                        assert not errors, errors
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                        name = f'{theme}-{width}-{mode}'
                        page.screenshot(path=str(output / f'{name}.png'), full_page=True)
                        trigger = page.get_by_role('button', name='项目工作区', exact=True)
                        trigger.focus()
                        trigger.press('Enter')
                        drawer = page.get_by_role('dialog')
                        drawer.wait_for(state='visible')

                        page.wait_for_function('getComputedStyle(document.querySelector("dialog")).transform === "none"')
                        page.get_by_role('button', name='进入负责对话', exact=True).first.wait_for()
                        geometry = drawer.evaluate('(n)=>({left:n.getBoundingClientRect().left,right:n.getBoundingClientRect().right,scroll:n.scrollWidth,width:n.clientWidth})')
                        assert geometry['left'] >= 0 and geometry['right'] <= width + 1, geometry
                        assert geometry['scroll'] <= geometry['width'] + 1, geometry
                        assert page.get_by_text('执行失败 · 可查看部分证据', exact=True).is_visible()
                        assert page.get_by_text('执行已完成 · 交付另查', exact=True).is_visible()

                        assert '本次子运行' in drawer.inner_text()
                        assert '输入确认不等于接受任务' in drawer.inner_text()
                        page.get_by_role('button', name='查看当前对话通信', exact=True).click()
                        page.get_by_text('已提交 · 输入尚未确认', exact=True).wait_for()
                        assert page.get_by_text('接收者已声明接受 · 交付另查', exact=True).is_visible()
                        assert page.get_by_text('尚无明确处理决定', exact=True).is_visible()
                        assert drawer.evaluate('(n)=>n.scrollWidth<=n.clientWidth+1')
                        page.screenshot(path=str(output / f'{name}-messages.png'), full_page=True)
                        page.get_by_role('button', name='更早的消息', exact=True).click()
                        page.get_by_text('旧负责对话', exact=True).wait_for()
                        assert page.get_by_text('接收者已暂缓', exact=True).is_visible()
                        page.get_by_role('button', name='查看当前对话通信', exact=True).click()
                        page.screenshot(path=str(output / f'{name}-open.png'), full_page=True)
                        page.keyboard.press('Escape')
                        page.wait_for_function('!document.querySelector("dialog")')
                        assert trigger.evaluate('(n)=>n===document.activeElement')
                        trigger.click()
                        page.get_by_role('button', name='进入负责对话', exact=True).first.click()
                        assert page.get_by_text('已进入负责对话：frontend', exact=True).is_visible()
                        page.wait_for_function('!document.querySelector("dialog")')
                        assert not errors, errors
                        results.append({'theme': theme, 'width': width, 'mode': mode, 'geometry': geometry, 'errors': errors})
                        page.close()
        finally:
            browser.close()

            server.shutdown()
            server.server_close()
    (output / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n')
    print(f'{len(results)} actual Chromium journeys passed; screenshots: {output}')


if __name__ == '__main__':
    main()
