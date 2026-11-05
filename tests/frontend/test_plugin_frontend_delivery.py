"""Optional plugin pages execute in the native host and serve real assets."""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import logging
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pytest

from lib.app.app_factory import create_base_app
from lib.static_serving import load_static_bytes, register_static_route


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


class AssetLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.urls = []
        self.scripts = []
        self.styles = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'script' and attrs.get('src'):
            self.urls.append(attrs['src'])
            self.scripts.append(attrs['src'])
        if tag == 'link' and attrs.get('rel') in ('stylesheet', 'modulepreload', 'preload'):
            self.urls.append(attrs['href'])
            if attrs.get('rel') == 'stylesheet':
                self.styles.append(attrs['href'])


@pytest.fixture
def plugin_page_app(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'plugins/tofu-trading'))
    from tofu_trading import web

    monkeypatch.setattr(web, 'trading_enabled', lambda: True)
    app = create_base_app('plugin-delivery', {'TESTING': True})
    from lib.http_transport.plugin_frontend import register_plugin_frontend
    register_plugin_frontend(app)
    app.register_blueprint(web.trading_pages_bp)

    async def offload(loop, filename):
        return await loop.run_in_executor(None, load_static_bytes, str(ROOT / 'static'), filename)

    register_static_route(app, offload=offload, timeout=10, logger=logging.getLogger(__name__))
    return app


@pytest.mark.parametrize('prefix', ['', '/proxy/15000'])
def test_trading_page_loads_all_declared_assets_in_native_host(plugin_page_app, prefix):
    async def run():
        client = plugin_page_app.test_client()
        response = await client.get('/trading.html')
        assert response.status_code == 200
        html = (await response.get_data()).decode()
        parsed = AssetLinks(html)
        assert parsed.scripts and parsed.styles
        assert parsed.styles.index('trading-static/trading.css') < parsed.styles.index('trading-static/theme-bridge.css')
        links = parsed.urls
        missing = []
        for relative in links:
            url = urlsplit(urljoin(f'http://localhost{prefix}/trading.html', relative))
            assert url.path.startswith(prefix + '/')
            asset = await client.get(url.path.removeprefix(prefix))
            if asset.status_code != 200:
                missing.append((relative, asset.status_code))
        assert missing == []

    asyncio.run(run())
