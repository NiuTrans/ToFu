"""Static contract for the classic Video renderer island."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')


def test_classic_video_is_registered_renderer_only():
    from lib.js_bundler import _CLASSIC_ASSET_FILES

    assert 'paper/video.js' in _CLASSIC_ASSET_FILES
    source = open(VIDEO_JS, encoding='utf-8').read()
    assert 'function _pvRender()' in source
    assert 'function _pvRenderSceneGrid' in source
    assert 'function _videoGenerate' not in source
    assert 'function _pvPoll' not in source


def test_video_surface_and_api_are_wired():
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    api = open(os.path.join(ROOT, 'static', 'js', 'api.js'), encoding='utf-8').read()
    assert html.count('data-tab="video"') >= 2
    assert 'paperVideoContent' in html
    for name in ('videoStart', 'videoLookup'):
        assert name in api
    for name in (
        'shotRecipes',
        'audioContract',
        'regenScene',
        'sceneFileUrl',
    ):
        assert name in api


def test_video_i18n_contract():
    source = open(
        os.path.join(ROOT, 'static', 'js', 'i18n.js'),
        encoding='utf-8',
    ).read()
    for key in (
        'paper.tabVideo',
        'paper.videoHint',
        'paper.videoGenerate',
        'paper.videoNoTts',
        'paper.videoNeedReport',
        'paper.videoRegen',
        'paper.videoScenesTitle',
    ):
        assert f"'{key}'" in source


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
