"""Contracts for the single-path Vite/ESM frontend."""

from __future__ import annotations

import json
from pathlib import Path
import re

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding='utf-8')


def test_vite_is_the_only_frontend_graph_and_has_two_entries():
    assert not (ROOT / 'static/js').exists()
    assert not (ROOT / 'lib/js_bundler.py').exists()
    assert not (ROOT / 'frontend/src/classic-assets.ts').exists()
    config = _read('vite.config.mjs')
    assert "target: 'safari15'" in config
    assert "main: resolve(process.cwd(), 'frontend/src/main.ts')" in config
    assert "admin: resolve(process.cwd(), 'frontend/src/admin.ts')" in config
    assert 'nomodule' not in config


def test_main_routes_domains_to_explicit_esm_owners():
    source = _read('frontend/src/main.ts')
    for domain in (
        'settings', 'memory', 'skills', 'paper', 'image', 'project-brain',
        'myday', 'misc', 'orchestration', 'infrastructure',
    ):
        assert f"import('./features/{domain}')" in source
    assert "import('./features/legacy')" not in source
    assert 'invokeLegacyFeature' not in source
    assert 'version: 3 as const' in source


def test_vendor_and_locale_owners_are_esm_chunks():
    vendor = _read('frontend/src/vendor-runtime.ts')
    for package in (
        'marked', 'dompurify', 'highlight.js', 'katex', 'html2canvas',
    ):
        assert package in vendor
    assert "import('katex')" in vendor
    assert "import('html2canvas')" in vendor
    paper_pdf = _read('frontend/src/features/paper/pdf-viewer.ts')
    assert "import('pdfjs-dist/legacy/build/pdf.mjs')" in paper_pdf
    assert "import('pdfjs-dist/legacy/build/pdf.worker.min.mjs?url')" in paper_pdf
    assert 'export function ensurePdfJs' in paper_pdf
    assert 'pdfJsLoading = undefined' in paper_pdf
    runtime = _read('frontend/src/runtime/app-runtime.js')
    assert 'loadPdfJs' not in runtime
    assert 'let pdfjsLib' not in runtime
    assert 'return runtimeScope[name] ?? runtimeActions[name]' in runtime
    i18n = _read('frontend/src/i18n/index.ts')
    assert "import('./locales/zh.json')" in i18n
    assert "import('./locales/en.json')" in i18n
    assert 'window.t' not in i18n


def test_html_has_no_classic_or_inline_event_loader():
    pages = ['index.html', 'static/admin.html']
    pages.extend(
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / 'static/settings_panels').glob('*.html'))
    )
    for relative in pages:
        source = _read(relative)
        assert 'static/js/' not in source
        assert 'nomodule' not in source
        if relative in ('index.html', 'static/admin.html'):
            assert 'data-tofu-action' in source
        for event in ('onclick=', 'onchange=', 'oninput=', 'onsubmit=', 'onkeydown='):
            assert event not in source.lower()
    registry = _read('frontend/src/action-registry.ts')
    assert 'new Function' not in registry
    assert 'eval(' not in registry


def test_manifest_validation_covers_standalone_url_assets(tmp_path, monkeypatch):
    import lib.vite_assets as assets

    out = tmp_path / 'vite'
    emitted = out / 'assets'
    emitted.mkdir(parents=True)
    for name in ('main.js', 'admin.js', 'worker.mjs'):
        (emitted / name).write_text('// emitted', encoding='utf-8')
    manifest = {
        'frontend/src/main.ts': {
            'file': 'assets/main.js', 'isEntry': True,
        },
        'frontend/src/admin.ts': {
            'file': 'assets/admin.js', 'isEntry': True,
        },
        'node_modules/vendor/worker.mjs': {
            'file': 'assets/worker.mjs',
        },
    }
    manifest_path = out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(assets, 'VITE_OUT_DIR', str(out))
    monkeypatch.setattr(assets, 'VITE_MANIFEST', str(manifest_path))
    assets.clear_vite_asset_cache()

    assert assets.validate_vite_artifact() == manifest
    assert 'assets/main.js' in assets.get_vite_asset_tags('main')
    assert 'assets/admin.js' in assets.get_vite_asset_tags('admin')
    (emitted / 'worker.mjs').unlink()
    with pytest.raises(assets.ViteAssetError, match='worker.mjs'):
        assets.validate_vite_artifact()


def test_release_wrapper_publishes_manifest_last_and_retains_previous_graph():
    source = _read('scripts/build_frontend.mjs')
    copy_at = source.index('for (const asset of nextAssets)')
    previous_at = source.index("'previous-manifest.json'")
    manifest_at = source.index("'manifest.json'), nextManifest")
    assert copy_at < previous_at < manifest_at
    assert 'for (const key of Object.keys(manifest)) await visit(key)' in source
    assert 'new Set([...nextAssets, ...previousAssets]' in source


def test_server_uses_prebuilt_manifest_and_never_runtime_bundles():
    route = _read('routes/common.py')
    server = _read('server.py')
    assert 'get_vite_asset_tags' in route
    assert "get_vite_asset_tags('admin')" in route
    assert 'js_bundler' not in route
    assert 'js_bundler' not in server
    assert 'subprocess' not in _read('lib/vite_assets.py')


def test_orchestration_helpers_stay_inside_the_module_graph():
    """Typed orchestration owners must not recreate the classic global graph."""
    root = ROOT / 'frontend/src/features/orchestration'
    for path in root.rglob('*.ts'):
        source = path.read_text(encoding='utf-8')
        assert 'Object.assign(window' not in source, path
        assert '(window as ' not in source, path
    registry = _read('frontend/src/features/orchestration/registry.ts')
    assert 'Object.create(null)' in registry


def test_window_public_surface_is_limited_to_api_and_tofu_modules():
    """Feature owners communicate through private registries, not window."""
    main = _read('frontend/src/main.ts')
    admin = _read('frontend/src/admin.ts')
    runtime = _read('frontend/src/runtime/app-runtime.js')
    assert 'window.TofuModules = Object.freeze({' in main
    assert 'publicWindow.Api = apiTransport' in admin
    assert 'global.Api = Api' in runtime
    assert 'Object.assign(window' not in runtime
    assert 'Object.defineProperty(window' not in runtime
    assert 'Object.defineProperties(window' not in runtime

    for path in (ROOT / 'frontend/src/features').rglob('*.ts'):
        source = path.read_text(encoding='utf-8')
        assert 'Object.assign(window' not in source, path
        assert 'return window as' not in source, path

    browser_native = {
        'addEventListener', 'alert', 'cancelAnimationFrame', 'clearInterval',
        'clearTimeout', 'confirm', 'devicePixelRatio', 'dispatchEvent',
        'getSelection', 'history', 'innerHeight', 'innerWidth',
        'IntersectionObserver', 'location', 'matchMedia', 'MutationObserver',
        'navigator', 'onerror', 'open', 'opener', 'PointerEvent', 'print',
        'prompt', 'removeEventListener', 'requestAnimationFrame',
        'ResizeObserver', 'scrollY', 'setInterval', 'setTimeout',
        'visualViewport',
    }
    allowed = browser_native | {'Api', 'TofuModules'}
    for path in (ROOT / 'frontend/src').rglob('*'):
        if path.suffix not in {'.js', '.ts'}:
            continue
        source = path.read_text(encoding='utf-8')
        source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
        source = re.sub(r'//[^\n]*', '', source)
        exposed = set(re.findall(r'\bwindow\.([A-Za-z_$][\w$]*)', source))
        assert exposed <= allowed, (path, sorted(exposed - allowed))

    transport = _read('frontend/src/api/transport.ts')
    assert 'globals.__TOFU_' not in transport
