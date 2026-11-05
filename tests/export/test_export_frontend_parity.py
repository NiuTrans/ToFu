"""Exported trees carry the frontend their own sources produce.

A tree published by ``export.py`` used to carry no ``static/vite`` (it is
gitignored build output), so every install of an export had to download
``frontend-dist-<VERSION>`` from a GitHub release. When that release asset
was never published (v0.18.2/v0.18.3), every fresh install failed outright;
an internal export installed on a corp host had no matching asset at all
because its sources differ from the public build.

The export now builds the Vite graph from the exported (sanitized) sources,
validates it against those same authoring inputs, and commits it into the
published tree, so install never needs Node or a release download. The
public catalog is also closed over its own references: strings used only by
a maintainer-only fragment that the open export drops are removed with it,
so the shipped i18n dead-key gate stays green.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip('export', reason='export.py is not shipped in opensource builds')

pytestmark = pytest.mark.unit


def _write_locales(root: Path, keys: dict[str, str]) -> None:
    locales = root / 'frontend/src/i18n/locales'
    locales.mkdir(parents=True)
    for language in ('en', 'zh'):
        (locales / f'{language}.json').write_text(
            json.dumps(keys, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


def test_open_catalog_drops_keys_only_an_excluded_fragment_used(tmp_path, monkeypatch):
    from export_pkg import _export_core

    source = tmp_path / 'source'
    panel = source / 'static/settings_panels/experiments.html'
    panel.parent.mkdir(parents=True)
    panel.write_text('<div data-i18n="settings.privateOnly">x</div>'
                     '<p data-i18n-title="settings.shared">y</p>', encoding='utf-8')
    dest = tmp_path / 'dest'
    _write_locales(dest, {'settings.privateOnly': 'P', 'settings.shared': 'S',
                          'settings.public': 'U'})
    shipped = dest / 'frontend/src/app.ts'
    shipped.write_text("t('settings.shared'); t('settings.public');", encoding='utf-8')
    monkeypatch.setattr(_export_core, 'ROOT', source)

    removed = _export_core._prune_open_catalog_of_excluded_fragments(dest)

    assert removed == ['settings.privateOnly']
    for language in ('en', 'zh'):
        text = (dest / f'frontend/src/i18n/locales/{language}.json').read_text(encoding='utf-8')
        assert json.loads(text) == {'settings.shared': 'S', 'settings.public': 'U'}
        # Same formatting as the authored file, so later regeneration is stable.
        assert text == json.dumps(json.loads(text), indent=2, ensure_ascii=False) + '\n'


def test_exported_frontend_is_built_from_the_exported_sources(tmp_path, monkeypatch):
    from export_pkg import _export_core

    dest = tmp_path / 'dest'
    (dest / 'scripts').mkdir(parents=True)
    (dest / 'scripts/build_frontend.mjs').write_text('// build', encoding='utf-8')
    (dest / '.gitignore').write_text('static/vite/\n', encoding='utf-8')
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs.get('cwd')))
        if command[-1].endswith('build_frontend.mjs'):
            (dest / 'static/vite').mkdir(parents=True)
            (dest / 'static/vite/manifest.json').write_text('{}', encoding='utf-8')

        class Done:
            returncode = 0
            stdout = ''
            stderr = ''
        return Done()

    monkeypatch.setattr(_export_core.subprocess, 'run', fake_run)
    monkeypatch.setattr(_export_core.shutil, 'which', lambda name: '/usr/bin/node')

    _export_core._build_exported_frontend(dest)

    build, verify = calls
    assert build[0][-1] == str(dest / 'scripts/build_frontend.mjs') and build[1] == dest
    assert verify[0][-2:] == [str(dest / 'scripts/verify_frontend_dist.py'),
                              '--authoring-freshness'] and verify[1] == dest
    ignore = (dest / '.gitignore').read_text(encoding='utf-8')
    assert '!/static/vite/' in ignore.split('static/vite/\n', 1)[1], (
        'the built graph must be publishable despite the source .gitignore')


def test_exported_frontend_never_embeds_the_export_host_path(tmp_path, monkeypatch):
    """A toolchain link escaping the tree made Rollup record absolute host
    paths in manifest.json; the public secret scan then blocked the export."""
    from export_pkg import _export_core

    source = tmp_path / 'source'
    (source / 'node_modules' / 'vite').mkdir(parents=True)
    dest = tmp_path / 'dest'
    (dest / 'scripts').mkdir(parents=True)
    (dest / 'scripts/build_frontend.mjs').write_text('// build', encoding='utf-8')
    monkeypatch.setattr(_export_core, 'ROOT', source)
    monkeypatch.setattr(_export_core.shutil, 'which', lambda name: '/usr/bin/node')
    seen = []

    def fake_run(command, **kwargs):
        toolchain = dest / 'node_modules'
        seen.append(toolchain.is_dir() and not toolchain.is_symlink())
        if command[-1].endswith('build_frontend.mjs'):
            (dest / 'static/vite').mkdir(parents=True)
            (dest / 'static/vite/manifest.json').write_text(
                json.dumps({str(source.resolve() / 'node_modules/vite/x.js'): {}}),
                encoding='utf-8')

        class Done:
            returncode = 0
            stdout = stderr = ''
        return Done()

    monkeypatch.setattr(_export_core.subprocess, 'run', fake_run)
    with pytest.raises(_export_core.ExportIntegrityError, match='host path'):
        _export_core._build_exported_frontend(dest)
    assert seen and all(seen), 'the toolchain must be a real in-tree copy, not a link'
    assert not (dest / 'node_modules').exists(), 'the temporary toolchain copy must not ship'


def test_export_without_node_fails_closed(tmp_path, monkeypatch):
    from export_pkg import _export_core

    dest = tmp_path / 'dest'
    (dest / 'scripts').mkdir(parents=True)
    (dest / 'scripts/build_frontend.mjs').write_text('// build', encoding='utf-8')
    monkeypatch.setattr(_export_core.shutil, 'which', lambda name: None)
    with pytest.raises(_export_core.ExportIntegrityError, match='Node'):
        _export_core._build_exported_frontend(dest)
