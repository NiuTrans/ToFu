from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_ensure_gsap_verifies_caches_and_stages(monkeypatch, tmp_path):
    from lib.motion_video import _runtime_assets as runtime

    data = b'// pinned gsap\n' * 32
    monkeypatch.setattr(runtime, 'GSAP_SHA256', hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(runtime, '_MIN_GSAP_BYTES', 32)
    monkeypatch.setattr('lib.motion_video._env.motion_root',
                        lambda: str(tmp_path / 'motion'))
    calls = []

    def fake_get(url, timeout):
        calls.append((url, timeout))
        return SimpleNamespace(status_code=200, content=data)

    monkeypatch.setattr('lib.http_client.http_get', fake_get)
    first = tmp_path / 'scene-a'
    second = tmp_path / 'scene-b'

    assert runtime.ensure_gsap(str(first)) == runtime.GSAP_REL_PATH
    assert runtime.ensure_gsap(str(second)) == runtime.GSAP_REL_PATH
    assert (first / runtime.GSAP_REL_PATH).read_bytes() == data
    assert (second / runtime.GSAP_REL_PATH).read_bytes() == data
    assert len(calls) == 1


def test_localise_gsap_rewrites_primary_and_network_fallback(monkeypatch,
                                                             tmp_path):
    from lib.motion_video import _runtime_assets as runtime

    monkeypatch.setattr(runtime, 'ensure_gsap',
                        lambda scene_dir: runtime.GSAP_REL_PATH)
    html = '''<html><head>
<script src="https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js"></script>
<script>if(typeof gsap==='undefined'){document.write('<script src="https://unpkg.com/gsap@3.12.5/dist/gsap.min.js"><\\/script>')}</script>
</head><body></body></html>'''

    out = runtime.localise_gsap_html(html, str(tmp_path))

    assert 'https://' not in out
    assert out.count(runtime.GSAP_REL_PATH) == 2


def test_localise_gsap_injects_runtime_when_author_omits_loader(monkeypatch,
                                                               tmp_path):
    from lib.motion_video import _runtime_assets as runtime

    monkeypatch.setattr(runtime, 'ensure_gsap',
                        lambda scene_dir: runtime.GSAP_REL_PATH)
    html = '<html><head></head><body><script>gsap.timeline()</script></body></html>'

    out = runtime.localise_gsap_html(html, str(tmp_path))

    assert f'<script src="{runtime.GSAP_REL_PATH}"></script>' in out
