"""Actual artifact/report responses render locked math inside opaque frames.

Storage and HTTP response construction use the application test fixtures.
An owned loopback origin replays their bytes/headers to Chromium. Only CDN
transport is replaced with the identical locked package bytes, including SRI.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests._http_asset_origin import serve_asset_responses

from lib.http_transport.generated_frontend_vendor import KATEX_STATIC_SHA256, KATEX_VERSION

pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def math_origin(flask_app, flask_client, monkeypatch):
    from lib import response_assets
    from lib.artifacts import create_artifact
    from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport

    monkeypatch.setattr(response_assets, 'get_conversation',
                        lambda conv, user_id, **kw: {'id': conv} if conv == 'math-view' and user_id == 1 else None)
    with flask_app.app_context():
        artifact = create_artifact(
            conv_id='math-view', source='write_file', title='Math view', format='html',
            content=r'<h1>Formula parity</h1><p>Inline $a_i^2$.</p>'
                    r'<p>$$\frac{1}{\sqrt{2}}$$</p><code>$literal$</code>'
                    '<button onclick="document.body.dataset.attack=1">Blocked</button>'
                    '<script>document.body.dataset.attack=1</script>')
    paper_hash = 'e1' * 32
    assert PaperArtifactRepository(1).put_report(
        PaperReport(paper_hash, 'en',
                    '# Formula parity\n\nInline $a_i^2$.\n\n'
                    r'$$\frac{1}{\sqrt{2}}$$' + '\n\n`$literal$`\n'),
        command_id='standalone-math-report',
    )
    paths = {
        '/artifact': f'/api/artifacts/{artifact["id"]}/view',
        '/report': f'/api/v4/paper/report/export?paper_hash={paper_hash}&lang=en&format=html&inline=1',
        **{f'/{path}': f'/{path}' for path in KATEX_STATIC_SHA256},
    }
    responses = {}
    for route, path in paths.items():
        response = flask_client.get(path, headers={'Origin': 'null'})
        assert response.status_code == 200, (path, response.status_code)
        responses[route] = (response.data, dict(response.headers))

    for surface in ('artifact', 'report'):
        body = ('<!doctype html><iframe id="preview" sandbox="allow-scripts" '
                f'style="width:100%;height:600px" src="/{surface}"></iframe>').encode()
        responses[f'/host/{surface}'] = (body, {'Content-Type': 'text/html; charset=utf-8'})
    with serve_asset_responses(responses) as origin:
        yield origin


@pytest.mark.parametrize('surface', ['artifact', 'report'])
def test_standalone_math_has_glyphs_and_keeps_opaque_origin(browser, math_origin, surface, tmp_path):
    page = browser.new_page()
    errors = []
    diagnostics = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda message: diagnostics.append(message.text) if message.type == 'error' else None)
    page.on('requestfailed', lambda request: diagnostics.append(f'{request.url}: {request.failure}'))
    prefix = f'https://cdn.jsdelivr.net/npm/katex@{KATEX_VERSION}/dist/'

    def cdn(route):
        relative = route.request.url.removeprefix(prefix)
        path = ROOT / 'node_modules/katex/dist' / relative
        assert path.is_file() and '..' not in Path(relative).parts, route.request.url
        kind = 'text/css' if path.suffix == '.css' else 'text/javascript' if path.suffix == '.js' else 'font/woff2'
        route.fulfill(body=path.read_bytes(), content_type=kind,
                      headers={'Access-Control-Allow-Origin': '*'})

    page.route(prefix + '**', cdn)
    try:
        page.goto(math_origin + '/host/' + surface)
        frame = page.frame_locator('#preview')
        expect(frame.locator('.katex')).to_have_count(2)
        expect(frame.locator('.katex-display')).to_have_count(1)
        expect(frame.locator('code')).to_have_text('$literal$')
        content = page.frames[1]
        assert content.evaluate('''() => {
            try { void parent.document.body; return false; }
            catch (error) { return error.name === 'SecurityError'; }
        }''')
        if surface == 'artifact':
            frame.get_by_role('button', name='Blocked').click()
            assert content.evaluate('document.body.dataset.attack') is None
        fonts = content.evaluate('''async () => {
            await document.fonts.load('16px KaTeX_Main');
            await document.fonts.ready;
            return {ready: document.fonts.check('16px KaTeX_Main'),
                errors: [...document.fonts].filter(face => face.status === 'error').map(face => face.family)};
        }''')
        assert fonts == {'ready': True, 'errors': []}
        assert not errors, errors
        screenshot = tmp_path / f'{surface}-math.png'
        page.screenshot(path=str(screenshot))
        print(f'math screenshot: {screenshot}')
    finally:
        print(f'{surface} browser errors: {errors}; console: {diagnostics}')
        page.close()
