"""Emission contracts for Vite's content-hashed locale data assets."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from lib.http_transport.ui_languages import UI_LANGUAGES
from lib.vite_assets import VITE_MANIFEST, validate_vite_artifact
from tests._jsdom import require_shipped_graph


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _manifest() -> dict:
    require_shipped_graph()
    validate_vite_artifact()
    return json.loads(Path(VITE_MANIFEST).read_text(encoding='utf-8'))


def _reachable_assets(manifest: dict, entry: str) -> set[str]:
    pending = [entry]
    visited: set[str] = set()
    assets: set[str] = set()
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        row = manifest.get(key)
        assert isinstance(row, dict), f'missing manifest dependency: {key}'
        assets.update(row.get('assets') or ())
        pending.extend(row.get('imports') or ())
    return assets


def _locale_assets(manifest: dict) -> dict[str, str]:
    assets = _reachable_assets(manifest, 'frontend/src/main.ts')
    matches = {}
    for language in UI_LANGUAGES:
        pattern = re.compile(
            rf'assets/{language}\.generated-[A-Za-z0-9_-]{{8,}}\.json',
        )
        matches[language] = next((asset for asset in assets if pattern.fullmatch(asset)), '')
    return matches


def test_vite_emits_every_locale_as_a_distinct_content_hashed_data_asset():
    manifest = _manifest()
    files = _locale_assets(manifest)
    assert all(files.values()), files
    assert len(set(files.values())) == len(UI_LANGUAGES)


def test_emitted_locale_assets_are_exact_valid_catalogs():
    manifest = _manifest()
    for language, asset in _locale_assets(manifest).items():
        emitted = json.loads(
            (Path(VITE_MANIFEST).parent / asset).read_text(encoding='utf-8'),
        )
        delivered = json.loads(
            (ROOT / f'frontend/src/i18n/generated/{language}.generated.json').read_text(
                encoding='utf-8',
            ),
        )
        assert emitted == delivered


def test_main_entry_references_locale_data_without_javascript_entries():
    manifest = _manifest()
    assert not any('i18n/locales/' in key for key in manifest)
    assert all(_locale_assets(manifest).values())


def test_classic_i18n_pack_emitter_is_gone():
    assert not (ROOT / 'lib/i18n_packs.py').exists()
    assert not (ROOT / 'static/js').exists()
