"""The QR page calls the current API under the browser's actual prefix."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess

import pytest

from routes.api_v1.mobile import _CONNECT_PAGE_HTML
from tests.api.test_mobile_connect_security import mobile_app
from tests._jsdom import frontend_module_guard

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('page_kind', ['connect', 'android'])
@pytest.mark.parametrize('page_url,root', [
    ('https://host.test/api/v4/mobile/connect', 'https://host.test/'),
    ('https://host.test:8443/team/proxy/15005/api/v4/mobile/connect',
     'https://host.test:8443/team/proxy/15005/'),
    ('https://host.test/team/api/service/absproxy/15005/api/v4/mobile/connect/',
     'https://host.test/team/api/service/absproxy/15005/'),
])
def test_connect_page_fetches_v4_through_exact_browser_prefix(tmp_path, page_url, root, page_kind):
    frontend_module_guard(need_jsdom=True)
    page_url = page_url.replace('/mobile/connect', '/mobile/' + page_kind)

    async def install_page():
        response = await mobile_app().test_client().get('/api/v4/mobile/android')
        assert response.status_code == 200
        return await response.get_data(as_text=True)

    html = _CONNECT_PAGE_HTML if page_kind == 'connect' else asyncio.run(install_page())
    harness = tmp_path / 'connect.cjs'
    harness.write_text(r'''
const assert = require('node:assert/strict');
const { JSDOM } = require(process.env.JSDOM_MODULE);
const input = JSON.parse(process.env.CONNECT_INPUT);
const dom = new JSDOM(input.html, {url: input.url, runScripts: 'outside-only'});
const calls = [];
dom.window.fetch = async (url, options) => {
  calls.push({url, options});
  return {ok: true, json: async () => ({data: {
    qr: 'data:image/png;base64,fixture',
    url: input.kind === 'connect' ? input.root : input.root + 'api/v4/mobile/android/apk',
    deepLink: 'tofu://connect?url=fixture'
  }, meta: {requestId: 'fixture'}})};
};
dom.window.eval(dom.window.document.querySelector('script').textContent);
setImmediate(() => {
  try {
    assert.equal(calls.length, 1);
    const url = new URL(calls[0].url, input.url);
    const expected = new URL('api/v4/mobile/' + input.kind + '/link', input.root);
    assert.equal(url.origin + url.pathname, expected.origin + expected.pathname);
    assert.equal(url.searchParams.get(input.kind === 'connect' ? 'url' : 'base'), input.root);
    assert.equal(calls[0].options.credentials, 'same-origin');
    assert.equal(calls[0].options.redirect, 'error');
    if (input.kind === 'connect') {
      assert.equal(dom.window.document.getElementById('u').textContent, input.root);
    } else {
      assert.equal(dom.window.document.getElementById('link').getAttribute('href'),
        input.root + 'api/v4/mobile/android/apk');
    }
    assert.equal(dom.window.document.getElementById('qr').getAttribute('src'), 'data:image/png;base64,fixture');
    assert.equal(dom.window.document.getElementById('err').textContent, '');
  } finally { dom.window.close(); }
});
''', encoding='utf-8')
    result = subprocess.run(['node', str(harness)], text=True, capture_output=True,
                            timeout=30, env={**os.environ,
                                'JSDOM_MODULE': str(ROOT / 'node_modules/jsdom'),
                                'CONNECT_INPUT': json.dumps({'html': html, 'kind': page_kind,
                                                             'url': page_url, 'root': root})})
    assert result.returncode == 0, result.stderr
