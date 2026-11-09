"""Real browser/HTTP verification of the phone setup page behind a proxy."""
from __future__ import annotations

import asyncio
import socket
import threading

import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config

from tests.api.test_mobile_connect_security import mobile_app

pytestmark = pytest.mark.visual


@pytest.fixture
def mobile_server():
    app = mobile_app()
    prefix = '/team/api/service/proxy/15005'
    state = {}
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]

    async def proxied(scope, receive, send):
        if scope['type'] == 'http' and scope['path'].startswith(prefix + '/'):
            scope = dict(scope, root_path=prefix)
        await app(scope, receive, send)

    def runner():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop = asyncio.Event()
        state.update(loop=loop, stop=stop)
        config = Config()
        config.bind = [f'127.0.0.1:{port}']
        config.accesslog = None
        config.errorlog = None
        state['ready'].set()
        try:
            loop.run_until_complete(serve(proxied, config, shutdown_trigger=stop.wait))
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    state['ready'] = threading.Event()
    thread = threading.Thread(target=runner, name='mobile-qr-test')
    thread.start()
    try:
        assert state['ready'].wait(5)
        yield f'http://127.0.0.1:{port}{prefix}/'
    finally:
        state['loop'].call_soon_threadsafe(state['stop'].set)
        thread.join(10)
        assert not thread.is_alive(), 'QR HTTP test producer must settle'


@pytest.mark.parametrize('page_kind', ['connect', 'android'])
@pytest.mark.parametrize('width,height', [(390, 844), (820, 1180)])
def test_qr_page_renders_current_prefix_in_real_browser(browser, mobile_server, tmp_path, width, height, page_kind):
    context = browser.new_context(viewport={'width': width, 'height': height}, service_workers='block')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        response = page.goto(mobile_server + 'api/v4/mobile/' + page_kind, wait_until='networkidle')
        assert response.status == 200, page.content()
        page.wait_for_function("document.getElementById('qr')?.naturalWidth > 0")
        if page_kind == 'connect':
            assert page.locator('#u').inner_text() == mobile_server
            assert page.locator('#d').inner_text().startswith('tofu://connect?url=')
        else:
            assert page.locator('#link').get_attribute('href') == mobile_server + 'api/v4/mobile/android/apk'
            assert '?token=' not in page.locator('#link').inner_text()
        assert page.locator('#err').inner_text() == ''
        assert page.locator('body').evaluate('(el) => el.scrollWidth <= innerWidth')
        assert errors == []
        page.screenshot(path=str(tmp_path / f'mobile-{page_kind}-{width}.png'), full_page=True)
    finally:
        context.close()
