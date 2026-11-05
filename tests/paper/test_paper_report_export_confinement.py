"""Paper report HTML exports confine model-authored markup to an opaque origin."""

from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import os

import pytest

from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport


pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]

_PAPER_HASH = 'a1' * 32
# Report bodies are LLM output over untrusted paper text; Python-Markdown
# passes raw HTML, attribute lists and javascript: links through unchanged.
_INJECTED_REPORT = (
    '# Findings {: onmouseover="alert(1)" }\n\n'
    '<img src=x onerror="fetch(\'/api/v4/credentials\')">\n\n'
    '<script>opener.document.title = document.cookie</script>\n\n'
    '[details](javascript:alert(3))\n'
)


def _load_app():
    os.environ.setdefault('TOFU_AUTH_MODE', 'open')
    from lib.storage import start_storage
    import server

    start_storage()
    server.app.config['TESTING'] = True
    return server.app


def _directives(policy: str) -> dict[str, list[str]]:
    parsed = {}
    for part in policy.split(';'):
        tokens = part.split()
        if tokens:
            parsed[tokens[0].lower()] = tokens[1:]
    return parsed


@pytest.mark.parametrize('query', ['format=pdf', 'format=html&inline=1', 'format=html'])
def test_report_html_export_confines_model_markup_to_an_opaque_origin(query):
    app = _load_app()
    assert PaperArtifactRepository(1).put_report(
        PaperReport(_PAPER_HASH, 'en', _INJECTED_REPORT),
        command_id='paper-export-confinement-seed',
    )

    async def run():
        async with app.test_client() as client:
            response = await client.get(
                f'/api/v4/paper/report/export?paper_hash={_PAPER_HASH}'
                f'&lang=en&{query}')
            assert response.status_code == 200
            assert response.mimetype == 'text/html'
            body = await response.get_data(as_text=True)
            assert 'onerror=' in body  # the export still carries the raw report
            return response.headers.get('Content-Security-Policy', '')

    policy = _directives(asyncio.run(run()))
    assert 'sandbox' in policy, 'model markup must not run on the app origin'
    assert 'allow-same-origin' not in policy['sandbox']
    assert policy.get('default-src') == ["'none'"]
    assert 'connect-src' not in policy
    assert policy.get('form-action') == ["'none'"]


def test_report_html_uses_locked_assets_with_integrity():
    from lib.http_transport.generated_frontend_vendor import KATEX_CDN_ASSETS

    app = _load_app()
    assert PaperArtifactRepository(1).put_report(
        PaperReport(_PAPER_HASH, 'en', '# Math\n\n$a_i^2$'),
        command_id='paper-export-locked-assets-seed',
    )

    async def run():
        async with app.test_client() as client:
            response = await client.get(
                f'/api/v4/paper/report/export?paper_hash={_PAPER_HASH}&lang=en&format=html')
            assert response.status_code == 200
            return await response.get_data(as_text=True)

    class AssetParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.assets = []

        def handle_starttag(self, tag, attrs):
            if tag in ('link', 'script'):
                self.assets.append(dict(attrs))

    parser = AssetParser()
    html = asyncio.run(run())
    assert '$a_i^2$' in html
    parser.feed(html)
    for asset in KATEX_CDN_ASSETS.values():
        tags = [tag for tag in parser.assets if tag.get('src', tag.get('href')) == asset['url']]
        assert len(tags) == 1
        assert tags[0]['integrity'] == asset['integrity']
        assert tags[0]['crossorigin'] == 'anonymous'


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage

    guard_standalone_storage('test_paper_report_export_confinement.standalone')
    raise SystemExit(pytest.main([__file__, '-v']))
