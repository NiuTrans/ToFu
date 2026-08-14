"""Static contract for the classic Podcast renderer island.

Task state, replay, cancellation and teardown are covered by the compiled
Podcast runtime tests; this file verifies the deliberately retained renderer.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')


def test_classic_podcast_is_registered_renderer_only():
    from lib.js_bundler import _CLASSIC_ASSET_FILES

    assert 'paper/podcast.js' in _CLASSIC_ASSET_FILES
    source = open(PODCAST_JS, encoding='utf-8').read()
    assert 'function _pcRender()' in source
    assert 'function _podcastSeekSegment' in source
    assert 'function _podcastGenerate' not in source
    assert 'function _pcPoll' not in source


def test_podcast_surface_and_api_are_wired():
    html = open(os.path.join(ROOT, 'index.html'), encoding='utf-8').read()
    api = open(os.path.join(ROOT, 'static', 'js', 'api.js'), encoding='utf-8').read()
    assert html.count('data-tab="podcast"') >= 2
    assert 'paperPodcastContent' in html
    for name in (
        'podcastStatus',
        'podcastStart',
        'podcastPoll',
        'podcastLookup',
        'podcastAbort',
        'podcastScript',
    ):
        assert name in api


def test_podcast_i18n_contract():
    source = open(
        os.path.join(ROOT, 'static', 'js', 'i18n.js'),
        encoding='utf-8',
    ).read()
    for key in (
        'paper.tabPodcast',
        'paper.podcastNoTts',
        'paper.podcastNeedReport',
        'paper.podcastSleepTimer',
        'paper.podcastDownloadAudio',
        'paper.podcastGenerate',
    ):
        assert f"'{key}'" in source


def test_podcast_renderer_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    checked = subprocess.run(
        ['node', '--check', PODCAST_JS],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
