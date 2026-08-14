"""Syntax and boot-capability ratchets for the shipped Vite graph."""

from __future__ import annotations

import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / 'index.html'


@pytest.mark.skipif(shutil.which('node') is None, reason='node not installed')
def test_every_emitted_javascript_asset_parses():
    """The exact immutable JS files reachable from the manifest must parse."""
    from lib.vite_assets import VITE_OUT_DIR, validate_vite_artifact

    manifest = validate_vite_artifact()
    root = Path(VITE_OUT_DIR)
    paths = sorted({
        root / row['file']
        for row in manifest.values()
        if str(row.get('file', '')).lower().endswith(('.js', '.mjs'))
    })
    assert len(paths) > 10, 'manifest sanity: expected entry, shared, and dynamic chunks'
    node = shutil.which('node')

    def check(path: Path):
        result = subprocess.run(
            [node, '--input-type=module', '--check'],
            input=path.read_text(encoding='utf-8'),
            capture_output=True, text=True, timeout=30,
        )
        return path.name, result.returncode, (result.stderr or result.stdout)[-500:]

    with ThreadPoolExecutor(max_workers=8) as pool:
        failures = [row for row in pool.map(check, paths) if row[1]]
    assert not failures, failures


def test_typescript_graph_is_strict_and_owned_by_vite():
    config = (ROOT / 'tsconfig.vite.json').read_text(encoding='utf-8')
    vite = (ROOT / 'vite.config.mjs').read_text(encoding='utf-8')
    package = (ROOT / 'package.json').read_text(encoding='utf-8')
    assert '"strict": true' in config
    assert '"noEmit": true' in config
    assert '"include": ["frontend/src/**/*.ts"]' in config
    assert "target: 'safari15'" in vite
    assert 'tsc -p tsconfig.vite.json' in package


def test_index_html_script_srcs_are_unique_and_not_classic():
    html = INDEX_HTML.read_text(encoding='utf-8')
    srcs = re.findall(r'<script\b[^>]*?\bsrc="([^"]+)"', html)
    paths = [source.split('?')[0] for source in srcs]
    duplicates = sorted({path for path in paths if paths.count(path) > 1})
    assert not duplicates
    assert not any(path.startswith('static/js/') for path in paths)
    assert html.count('<!-- TOFU_APP_ASSETS -->') == 1


def test_boot_reports_module_failures_instead_of_dead_clicking_silently():
    html = INDEX_HTML.read_text(encoding='utf-8')
    main = (ROOT / 'frontend/src/main.ts').read_text(encoding='utf-8')
    assert "window.addEventListener('tofu:app-failed'" in html
    assert "window.addEventListener('tofu:app-ready'" in html
    assert "startup timed out after 20 seconds" in html
    assert 'Promise.all([i18nReady(), runtimeReady])' in main
    assert "new CustomEvent('tofu:app-ready'" in main
    assert "new CustomEvent('tofu:app-failed'" in main
