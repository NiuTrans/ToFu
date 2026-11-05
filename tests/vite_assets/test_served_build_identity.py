"""A newly served page must not be told by its first pong to reload old code."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from lib import vite_assets

pytestmark = pytest.mark.unit


@pytest.fixture
def publish(tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_VITE_DEV_SERVER', raising=False)
    monkeypatch.setattr(vite_assets, 'VITE_OUT_DIR', str(tmp_path))
    monkeypatch.setattr(vite_assets, 'VITE_MANIFEST', str(tmp_path / 'manifest.json'))
    (tmp_path / 'assets').mkdir()
    vite_assets.clear_vite_asset_cache()

    def write(generation):
        manifest = {}
        for name, entry in vite_assets.VITE_ENTRIES.items():
            filename = f'assets/{name}-{generation}.js'
            (tmp_path / filename).write_text('// fixture entry\n')
            manifest[entry] = {'file': filename, 'isEntry': True,
                vite_assets.I18N_CATALOG_DIGEST_FIELD: '0' * 64,
                vite_assets.VITE_AUTHORING_DIGEST_FIELD: '1' * 64}
        temporary = tmp_path / 'next-manifest.json'
        temporary.write_text(json.dumps(manifest))
        temporary.replace(tmp_path / 'manifest.json')
        return manifest

    yield write
    vite_assets.clear_vite_asset_cache()


def test_served_html_and_first_health_or_pong_share_one_validated_identity(publish, monkeypatch):
    publish('old')
    vite_assets.validate_published_vite_artifact()
    assert vite_assets.get_vite_build_id() == 'main-old.js'
    publish('new-generation')

    assert 'main-new-generation.js' in vite_assets.get_vite_asset_tags()
    assert vite_assets.get_vite_build_id() == 'main-new-generation.js'
    assert vite_assets.get_vite_build_id('admin') == 'admin-new-generation.js'

    # The page already validated this graph. A subsequent background check
    # needs only its existing stat; it must not revalidate every asset again.
    monkeypatch.setattr(vite_assets, '_load_manifest', lambda *a, **kw: pytest.fail('duplicate graph read'))
    assert vite_assets.refresh_vite_build_ids() == 'main-new-generation.js'


def test_failed_page_validation_keeps_the_previous_validated_identity(publish):
    publish('old')
    vite_assets.validate_published_vite_artifact()
    manifest = publish('incomplete-generation')
    from pathlib import Path
    Path(vite_assets.VITE_OUT_DIR, manifest[vite_assets.VITE_ENTRY]['file']).unlink()
    with pytest.raises(vite_assets.ViteAssetError):
        vite_assets.get_vite_asset_tags()
    assert vite_assets.get_vite_build_id() == 'main-old.js'


def test_older_background_validation_cannot_revert_a_newly_served_page(publish, monkeypatch):
    publish('old')
    vite_assets.validate_published_vite_artifact()
    captured, release = threading.Event(), threading.Event()
    real_load = vite_assets._load_manifest

    def load(*args, **kwargs):
        result = real_load(*args, **kwargs)
        if threading.current_thread().name.startswith('old-build'):
            captured.set()
            assert release.wait(10), 'test must release the owned background read'
        return result

    monkeypatch.setattr(vite_assets, '_load_manifest', load)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='old-build') as executor:
        future = executor.submit(vite_assets.refresh_vite_build_ids)
        try:
            assert captured.wait(10), 'background read must enter the old generation'
            publish('new-generation')
            assert 'main-new-generation.js' in vite_assets.get_vite_asset_tags()
        finally:
            release.set()
        future.result(timeout=10)
    assert vite_assets.get_vite_build_id() == 'main-new-generation.js'
