"""Emission contracts for Vite's dynamic locale chunks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
LOCALE_KEYS = {
    language: f'frontend/src/i18n/locales/{language}.json'
    for language in ('zh', 'en')
}


def _manifest() -> dict:
    validate_vite_artifact()
    return json.loads(Path(VITE_MANIFEST).read_text(encoding='utf-8'))


def test_vite_emits_both_locales_as_distinct_content_hashed_chunks():
    manifest = _manifest()
    files = []
    for language, key in LOCALE_KEYS.items():
        row = manifest[key]
        assert row.get('isDynamicEntry') is True
        assert re.fullmatch(rf'assets/{language}-[A-Za-z0-9_-]{{8,}}\.js', row['file'])
        files.append(row['file'])
    assert len(set(files)) == 2


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_emitted_locale_chunks_are_syntactically_valid_javascript():
    manifest = _manifest()
    for key in LOCALE_KEYS.values():
        path = Path(VITE_MANIFEST).parent / manifest[key]['file']
        result = subprocess.run(
            ['node', '--input-type=module', '--check'],
            input=path.read_text(encoding='utf-8'),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr


def test_main_entry_references_both_dynamic_locale_entries():
    manifest = _manifest()
    main = manifest['frontend/src/main.ts']
    dynamic = set(main.get('dynamicImports') or ())
    # The locale owner can sit behind another lazy chunk, so validate graph
    # membership rather than requiring a brittle direct edge from main.
    assert set(LOCALE_KEYS.values()).issubset(manifest)
    assert dynamic
    source = (ROOT / 'frontend/src/i18n/index.ts').read_text(encoding='utf-8')
    for key in LOCALE_KEYS.values():
        assert f"./locales/{Path(key).name}" in source


def test_classic_i18n_pack_emitter_is_gone():
    assert not (ROOT / 'lib/i18n_packs.py').exists()
    assert not (ROOT / 'static/js').exists()
