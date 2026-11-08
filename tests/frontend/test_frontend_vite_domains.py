"""Contracts for the single-path Vite/ESM frontend."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

from tests._jsdom import require_node_modules, require_shipped_graph


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding='utf-8')


def test_vite_uses_the_declared_frontend_entries():
    from lib.vite_assets import VITE_ENTRIES

    assert not (ROOT / 'static/js').exists()
    assert not (ROOT / 'lib/js_bundler.py').exists()
    assert not (ROOT / 'frontend/src/classic-assets.ts').exists()
    require_node_modules('vite')
    result = subprocess.run([
        'node', '--input-type=module', '-e',
        "import config from './vite.config.mjs'; "
        "process.stdout.write(JSON.stringify({target:config.build.target,entries:config.build.rollupOptions.input}));",
    ], cwd=ROOT, capture_output=True, text=True, check=True, timeout=30)
    config = json.loads(result.stdout)
    assert config['target'] == 'safari15'
    assert config['entries'] == {name: str(ROOT / source) for name, source in VITE_ENTRIES.items()}


def test_main_routes_domains_to_explicit_esm_owners():
    source = _read('frontend/src/application.ts')
    for domain in (
        'settings', 'memory', 'skills', 'paper', 'image', 'project-brain',
        'myday', 'misc', 'orchestration', 'infrastructure',
    ):
        assert f"import('./features/{domain}')" in source
    assert "import('./features/legacy')" not in source
    assert 'invokeLegacyFeature' not in source
    assert 'version: 3 as const' in source


def test_compact_bar_has_no_ambient_scene_dependency():
    source = _read('frontend/src/application.ts')
    assert 'pocket-theatre' not in source
    assert 'TofuPet' not in source
    assert './runtime/scene/' not in source
    shell = _read('index.html')
    assert 'id="sceneSwitchBtn"' not in shell
    assert 'id="convStatusStrip"' in shell


def test_vendor_and_locale_owners_are_esm_chunks():
    require_shipped_graph()
    vendor = _read('frontend/src/vendor-runtime.ts')
    for package in (
        'highlight.js', 'katex', 'html2canvas',
    ):
        assert package in vendor
    assert "import('katex')" in vendor
    assert "import('html2canvas')" in vendor
    # pdfjs stays a lazy chunk: the built graph must load the library and its
    # worker only as dynamic imports of the pdf-reader chunk, never eagerly.
    from lib.vite_assets import validate_vite_artifact
    manifest = validate_vite_artifact()
    pdf_reader = next(
        row for key, row in manifest.items()
        if key.startswith('_pdf-reader-') and key.endswith('.js'))
    # Vite resolves linked dependencies to their real path in isolated worktrees.
    # Assert the exact installed owner and worker, independent of checkout prefix.
    dynamic = {
        ((ROOT / key.partition('?')[0]).resolve(), key.partition('?')[2])
        for key in pdf_reader.get('dynamicImports') or ()
    }
    assert ((ROOT / 'node_modules/pdfjs-dist/legacy/build/pdf.mjs').resolve(), '') in dynamic
    assert ((ROOT / 'node_modules/pdfjs-dist/legacy/build/pdf.worker.min.mjs').resolve(), 'url') in dynamic
    runtime = _read('frontend/src/runtime/app-runtime.js')
    assert 'loadPdfJs' not in runtime
    assert 'let pdfjsLib' not in runtime
    assert 'return runtimeScope[name] ?? runtimeActions[name]' in runtime
    i18n = _read('frontend/src/i18n/index.ts')
    # Each language's catalog URL import is proven on the built graph by
    # tests/i18n/test_i18n_pack_emission.py (one hashed data asset each).
    assert "from './languages.generated'" in i18n
    assert "import('./locales/" not in i18n
    assert '.catalogUrl, {' in i18n
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
    for name in ('main.js', 'admin.js', 'plugin.js', 'worker.mjs', 'catalog.json'):
        (emitted / name).write_text('// emitted', encoding='utf-8')
    manifest = {
        'frontend/src/main.ts': {
            'file': 'assets/main.js', 'isEntry': True,
            'assets': ['assets/catalog.json'],
            assets.I18N_CATALOG_DIGEST_FIELD:
                assets._source_i18n_catalog_digest(),
            assets.VITE_AUTHORING_DIGEST_FIELD:
                assets._source_vite_authoring_digest(),
        },
        'frontend/src/admin.ts': {
            'file': 'assets/admin.js', 'isEntry': True,
        },
        'frontend/src/plugin.ts': {'file': 'assets/plugin.js', 'isEntry': True},
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
    main_tags = assets.get_vite_asset_tags('main')
    assert 'assets/main.js' in main_tags
    assert 'catalog.json' not in main_tags
    assert 'assets/admin.js' in assets.get_vite_asset_tags('admin')
    manifest['frontend/src/main.ts'][assets.I18N_CATALOG_DIGEST_FIELD] = '0' * 64
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assets.clear_vite_asset_cache()
    with pytest.raises(assets.ViteAssetError, match='i18n chunks are stale'):
        assets.validate_vite_artifact()
    manifest['frontend/src/main.ts'][assets.I18N_CATALOG_DIGEST_FIELD] = (
        assets._source_i18n_catalog_digest())
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assets.clear_vite_asset_cache()
    (emitted / 'worker.mjs').unlink()
    with pytest.raises(assets.ViteAssetError, match='worker.mjs'):
        assets.validate_vite_artifact()


def test_locale_source_change_preserves_requests_but_fails_deploy_validation(
        tmp_path, monkeypatch):
    import lib.vite_assets as assets

    out = tmp_path / 'vite'
    emitted = out / 'assets'
    emitted.mkdir(parents=True)
    (emitted / 'main.js').write_text('// emitted', encoding='utf-8')
    locales = tmp_path / 'locales'
    locales.mkdir()
    locale_paths = tuple(locales / f'{language}.json' for language in ('zh', 'en'))
    for path in locale_paths:
        path.write_text('{"contract.key":"value"}\n', encoding='utf-8')
    monkeypatch.setattr(assets, 'VITE_OUT_DIR', str(out))
    monkeypatch.setattr(assets, 'I18N_LOCALE_PATHS', tuple(map(str, locale_paths)))
    manifest = {
        'frontend/src/main.ts': {
            'file': 'assets/main.js',
            'isEntry': True,
            assets.I18N_CATALOG_DIGEST_FIELD:
                assets._source_i18n_catalog_digest(),
            assets.VITE_AUTHORING_DIGEST_FIELD: 'a' * 64,
        },
    }
    manifest_path = out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    monkeypatch.setattr(assets, 'VITE_MANIFEST', str(manifest_path))
    assets.clear_vite_asset_cache()

    expected_tags = assets.get_vite_asset_tags('main')
    assert 'assets/main.js' in expected_tags
    locale_paths[1].write_text('{"contract.key":"changed-and-longer"}\n', encoding='utf-8')

    # A source edit is not a deployment and cannot withdraw the last atomically
    # published graph from hard-refresh requests, whether the tag cache is warm
    # or cold. The next lifecycle validation remains strict and forces a build.
    assert assets.get_vite_asset_tags('main') == expected_tags
    assets.clear_vite_asset_cache()
    assert assets.get_vite_asset_tags('main') == expected_tags
    with pytest.raises(assets.ViteAssetError, match='i18n chunks are stale'):
        assets.validate_vite_artifact(('main',))


def _publish_graph(out: Path, chunks: list[str]) -> Path:
    """Atomically publish every declared entry plus ``chunks`` like the build."""
    import lib.vite_assets as assets

    emitted = out / 'assets'
    emitted.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name, source in assets.VITE_ENTRIES.items():
        (emitted / f'{name}.js').write_text('// entry', encoding='utf-8')
        manifest[source] = {'file': f'assets/{name}.js', 'isEntry': True}
    (emitted / 'shared.css').write_text('body {}', encoding='utf-8')
    for chunk in chunks:
        (emitted / f'{chunk}.js').write_text('// chunk', encoding='utf-8')
        manifest[f'_{chunk}.js'] = {
            'file': f'assets/{chunk}.js', 'css': ['assets/shared.css']}
    manifest[assets.VITE_ENTRY].update({
        'imports': [f'_{chunk}.js' for chunk in chunks],
        assets.I18N_CATALOG_DIGEST_FIELD: 'a' * 64,
        assets.VITE_AUTHORING_DIGEST_FIELD: 'b' * 64,
    })
    staged = out / 'manifest.json.next'
    staged.write_text(json.dumps(manifest), encoding='utf-8')
    os.replace(staged, out / 'manifest.json')
    return out / 'manifest.json'


def _serve_graph(monkeypatch, out: Path, manifest: Path) -> None:
    import lib.vite_assets as assets

    monkeypatch.delenv('TOFU_VITE_DEV_SERVER', raising=False)
    monkeypatch.setattr(assets, 'VITE_OUT_DIR', str(out))
    monkeypatch.setattr(assets, 'VITE_MANIFEST', str(manifest))
    assets.clear_vite_asset_cache()


def test_page_tag_validation_checks_each_distinct_file_once(
        tmp_path, monkeypatch):
    """GET / proves each distinct published file with one metadata call.

    On a network mount every metadata call re-acquires the GIL behind busy
    serving threads, so per-reference path walks repeated for every manifest
    occurrence made a hard refresh wait minutes for its HTML.
    """
    import lib.vite_assets as assets

    real_lstat, real_stat = os.lstat, os.stat
    calls: list[str] = []

    def counting_lstat(*args, **kwargs):
        calls.append('lstat')
        return real_lstat(*args, **kwargs)

    def counting_stat(*args, **kwargs):
        calls.append('stat')
        return real_stat(*args, **kwargs)

    metadata_calls = {}
    for count in (2, 64):
        out = tmp_path / f'graph{count:03d}'
        _serve_graph(monkeypatch, out, _publish_graph(
            out, [f'chunk-{index:03d}' for index in range(count)]))
        calls.clear()
        with monkeypatch.context() as patched:
            patched.setattr(os, 'lstat', counting_lstat)
            patched.setattr(os, 'stat', counting_stat)
            tags = assets.get_vite_asset_tags('main')
        assert f'assets/chunk-{count - 1:03d}.js' in tags
        metadata_calls[count] = len(calls)
    # Every chunk row also repeats one shared stylesheet: repeated references
    # are free and each additional distinct file costs exactly one call.
    assert metadata_calls[64] - metadata_calls[2] == 64 - 2


def test_page_tags_follow_manifest_publications(tmp_path, monkeypatch):
    import lib.vite_assets as assets

    out = tmp_path / 'vite'
    manifest = _publish_graph(out, ['chunk-a'])
    _serve_graph(monkeypatch, out, manifest)
    loads = []
    load_manifest = assets._load_manifest

    def counting_load(*args, **kwargs):
        loads.append(args[0])
        return load_manifest(*args, **kwargs)

    monkeypatch.setattr(assets, '_load_manifest', counting_load)

    main_tags = assets.get_vite_asset_tags('main')
    admin_tags = assets.get_vite_asset_tags('admin')
    # One validated publication keeps serving every entry's page; another
    # entry's request does not evict it.
    assert assets.get_vite_asset_tags('main') == main_tags
    assert assets.get_vite_asset_tags('admin') == admin_tags
    assert loads == [('main',), ('admin',)]

    # Coarse-mtime mounts can repeat mtime and size; the renamed manifest is
    # still a new publication and the very next page must serve it.
    before = os.stat(manifest)
    _publish_graph(out, ['chunk-b'])
    os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(manifest)
    assert (after.st_size, after.st_mtime_ns) == (
        before.st_size, before.st_mtime_ns)
    republished = assets.get_vite_asset_tags('main')
    assert 'assets/chunk-b.js' in republished
    assert 'assets/chunk-a.js' not in republished


@pytest.mark.skipif(not hasattr(os, 'symlink'), reason='needs symlinks')
@pytest.mark.parametrize('link', ['escaping-file', 'escaping-directory', 'internal-file'])
def test_page_tag_validation_keeps_symlink_containment(tmp_path, monkeypatch, link):
    import lib.vite_assets as assets

    outside = tmp_path / 'outside'
    outside.mkdir()
    out = tmp_path / 'vite'
    if link == 'escaping-directory':
        out.mkdir()
        (out / 'assets').symlink_to(outside, target_is_directory=True)
    manifest = _publish_graph(out, ['chunk-a'])
    chunk = out / 'assets' / 'chunk-a.js'
    if link == 'escaping-file':
        (outside / 'chunk-a.js').write_text('// unpublished', encoding='utf-8')
        chunk.unlink()
        chunk.symlink_to(outside / 'chunk-a.js')
    elif link == 'internal-file':
        (out / 'assets' / 'chunk-target.js').write_text('// chunk', encoding='utf-8')
        chunk.unlink()
        chunk.symlink_to(out / 'assets' / 'chunk-target.js')
    _serve_graph(monkeypatch, out, manifest)

    if link == 'internal-file':
        assert 'assets/chunk-a.js' in assets.get_vite_asset_tags('main')
    else:
        with pytest.raises(assets.ViteAssetError, match='unsafe Vite asset path'):
            assets.get_vite_asset_tags('main')


def test_vite_authoring_inputs_have_one_machine_readable_authority():
    contract = json.loads(_read('frontend/authoring-inputs.json'))
    assert 'scripts/check_frontend_tdz.mjs' in contract['configPaths']
    assert 'scripts/frontend_bundle_canary.py' in contract['configPaths']
    assert 'frontend/authoring-inputs.json' in contract['configPaths']
    build = _read('scripts/build_frontend.mjs')
    backend = _read('lib/vite_assets.py')
    assert "join(root, 'frontend', 'authoring-inputs.json')" in build
    assert "BASE_DIR, 'frontend', 'authoring-inputs.json'" in backend


def test_server_uses_prebuilt_manifest_and_never_runtime_bundles():
    route = _read('routes/common.py')
    server = _read('server.py')
    assert 'get_vite_asset_tags' in route
    assert 'get_vite_asset_tags("admin")' in route
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


def _window_references(sources):
    require_node_modules('typescript')
    result = subprocess.run(['node', str(ROOT / 'tests/frontend/_window_references.mjs')],
                            input=json.dumps(sources), capture_output=True,
                            text=True, check=True, cwd=ROOT, timeout=30)
    return json.loads(result.stdout)


def test_window_scan_resolves_local_bindings_and_ignores_noncode():
    source = '''
        window.Api;
        function page(window) { return window.laneId; }
        const nested = state.window.nextBeforeOrdinal;
        const text = "window.in_a_string";
        // window.in_a_comment
        function hoisted() { window.local; var window; }
        { const window = {}; window.block; }
        window.TofuModules;
        window.AccidentalGlobal;
    '''
    scan = _window_references({'probe.ts': source})
    assert scan['references'] == {'probe.ts': ['AccidentalGlobal', 'Api', 'TofuModules']}
    assert {'AudioContext', 'MediaRecorder', 'localStorage'} <= set(scan['native'])
    assert not {'Api', 'TofuModules', 'laneId', 'AccidentalGlobal'} & set(scan['native'])


def test_window_public_surface_is_limited_to_api_and_tofu_modules():
    """Feature owners communicate through private registries, not window."""
    main = _read('frontend/src/application.ts')
    admin = _read('frontend/src/admin.ts')
    runtime = _read('frontend/src/runtime/app-runtime.js')
    assert 'window.TofuModules = Object.freeze({' in main
    assert 'publicWindow.Api = apiTransport' in admin
    # The retained runtime consumes the private ESM transport; it need not
    # publish an additional global API alias.
    assert 'Object.assign(window' not in runtime
    assert 'Object.defineProperty(window' not in runtime
    assert 'Object.defineProperties(window' not in runtime

    for path in (ROOT / 'frontend/src/features').rglob('*.ts'):
        if path.name.endswith('.test.ts') or 'testing' in path.relative_to(ROOT / 'frontend/src').parts:
            continue
        source = path.read_text(encoding='utf-8')
        assert 'Object.assign(window' not in source, path
        assert 'return window as' not in source, path

    sources = {}
    for path in (ROOT / 'frontend/src').rglob('*'):
        if path.suffix not in {'.js', '.ts'} or path.name.endswith('.test.ts') or 'testing' in path.relative_to(ROOT / 'frontend/src').parts:
            continue
        sources[str(path)] = path.read_text(encoding='utf-8')
    scan = _window_references(sources)
    allowed = set(scan['native']) | {'Api', 'TofuModules'}
    for path, properties in scan['references'].items():
        exposed = set(properties)
        assert exposed <= allowed, (path, sorted(exposed - allowed))

    transport = _read('frontend/src/api/transport.ts')
    assert 'globals.__TOFU_' not in transport


def test_bundle_canary_serves_the_boot_required_owner_delegation_state():
    """Boot reads need a complete fixture or every build fails before upload."""
    from scripts.frontend_bundle_canary import _api_body

    state = _api_body('/api/v4/router/owner-delegation')['data']
    assert state['active'] is False
    assert state['grant'] is None
    assert state['actions'] == []
