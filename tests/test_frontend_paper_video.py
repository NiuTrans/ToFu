"""Contracts for the retained Video renderer and its native Vite runtime."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_names, runtime_section_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
VIDEO_JS = Path(runtime_section_path('paper/video.js', scope_prelude=False))
API_JS = Path(runtime_section_path('api.js', scope_prelude=False))
VIDEO_RUNTIME = ROOT / 'frontend' / 'src' / 'features' / 'paper' / 'video-runtime.ts'
PAPER_FEATURE = ROOT / 'frontend' / 'src' / 'features' / 'paper.ts'


def test_retained_video_is_registered_renderer_only():
    assert 'paper/video.js' in runtime_section_names()
    assert "import('./paper/video-runtime')" in PAPER_FEATURE.read_text(
        encoding='utf-8')
    source = VIDEO_JS.read_text(encoding='utf-8')
    runtime = VIDEO_RUNTIME.read_text(encoding='utf-8')
    assert 'function _pvRender()' in source
    assert 'function _pvRenderSceneGrid' in source
    assert 'function _videoGenerate' not in source
    assert 'function _pvPoll' not in source
    assert 'export async function generateVideo' in runtime
    assert 'export async function pollVideoOnce' in runtime


def test_video_surface_and_api_are_wired():
    html = (ROOT / 'index.html').read_text(encoding='utf-8')
    api = API_JS.read_text(encoding='utf-8')
    runtime = VIDEO_RUNTIME.read_text(encoding='utf-8')
    renderer = VIDEO_JS.read_text(encoding='utf-8')
    assert html.count('data-tab="video"') >= 2
    assert 'paperVideoContent' in html
    for name in ('videoStart', 'videoLookup'):
        assert name in api
        assert name in runtime
    for name in (
        'shotRecipes',
        'audioContract',
        'regenScene',
        'sceneFileUrl',
    ):
        assert name in api
    assert 'regenScene' in runtime
    assert 'sceneFileUrl' in renderer


def test_video_i18n_contract():
    keys = (
        'paper.tabVideo',
        'paper.videoHint',
        'paper.videoGenerate',
        'paper.videoNoTts',
        'paper.videoNeedReport',
        'paper.videoRegen',
        'paper.videoScenesTitle',
    )
    for locale in ('en', 'zh'):
        source = json.loads((
            ROOT / 'frontend' / 'src' / 'i18n' / 'locales' / f'{locale}.json'
        ).read_text(encoding='utf-8'))
        assert all(key in source for key in keys)


def test_video_renderer_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    checked = subprocess.run(
        ['node', '--check', VIDEO_JS],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
