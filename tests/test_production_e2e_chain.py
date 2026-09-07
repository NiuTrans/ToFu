"""tests/test_production_e2e_chain.py — the whole chain, wired for real.

Every other suite in this family tests ONE layer with the others faked. This
one drives the actual composition — recipe → substrate stage graph →
checkpoint → the engine's own storyboard gate → job manifest → crash-resume
rescan — so a break at a SEAM (rather than inside a module) is caught.

Only the three external seams are faked (web search / LLM / TTS). Everything
between them is the real code path a `produce_video(topic=…)` call takes
before rendering starts.

Written after P4–P7 landed: each phase was unit-tested in isolation, but the
composition had only ever been exercised by hand. This closes that gap.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit

from lib.motion_video import _recipe as rec

_FAKE_RESULTS = [
    {'title': 'Rayleigh scattering', 'url': 'https://example.org/rayleigh',
     'snippet': 'Shorter blue wavelengths scatter far more than red ones.'},
    {'title': 'Atmosphere basics', 'url': 'https://sci.example.com/atmo',
     'snippet': 'Air molecules scatter incoming sunlight across the sky.'},
]


@pytest.fixture
def wired(monkeypatch):
    """Fake ONLY the three external seams; everything else runs for real."""
    monkeypatch.setattr(rec, '_web_search',
                        lambda q, user_question='', freshness='': list(_FAKE_RESULTS))
    monkeypatch.setattr(rec, '_llm_chat', lambda m, **k: (json.dumps(
        {'title': '天空为什么是蓝色',
         'segments': ['阳光进入大气层。', '蓝光波长短,被散射得更多。']},
        ensure_ascii=False), {'total_tokens': 50}))
    monkeypatch.setattr(rec, '_tts_durations',
                        lambda scenes, out_dir, **k: {'ok': False,
                                                      'degraded': True})


def test_topic_to_engine_ready_storyboard(wired, tmp_path):
    """topic → scenes.json that the ENGINE's own gate accepts.

    The gate is the real one (``check_storyboard``) the engine runs before
    rendering, so this catches a recipe that produces a technically-valid JSON
    the engine would then reject.
    """
    out = rec.build_scenes_from_topic('天空为什么是蓝色', str(tmp_path), lang='zh')
    scenes = json.load(open(out['scenes_path'], encoding='utf-8'))

    assert len(scenes) >= 3                       # 2 script + sources card
    # Contiguous, monotonic timeline — a gap here desyncs audio from picture.
    for a, b in zip(scenes, scenes[1:]):
        assert abs(a['end'] - b['start']) < 1e-6
        assert b['end'] > b['start']
    assert '资料来源' in scenes[-1]['text']        # 片尾来源卡 (拍板 #4)

    from lib import motion_video as mv
    errs = mv.check_storyboard(scenes, (scenes[0]['start'], scenes[-1]['end']))
    assert errs == [], f'engine storyboard gate rejected the recipe output: {errs}'


def test_checkpoint_is_written_and_honoured_across_calls(wired, tmp_path,
                                                         monkeypatch):
    """A second run must re-search NOTHING — the crash-resume contract holds
    across the whole chain, not just inside run_stages."""
    rec.build_scenes_from_topic('天空为什么是蓝色', str(tmp_path), lang='zh')
    assert os.path.isfile(os.path.join(str(tmp_path), 'pipeline_state.json'))

    calls = {'n': 0}
    monkeypatch.setattr(rec, '_web_search', lambda q, user_question='': (
        calls.__setitem__('n', calls['n'] + 1), list(_FAKE_RESULTS))[1])
    monkeypatch.setattr(rec, '_llm_chat', lambda m, **k: (
        calls.__setitem__('n', calls['n'] + 100), ('{}', {}))[1])

    rec.build_scenes_from_topic('天空为什么是蓝色', str(tmp_path), lang='zh')
    assert calls['n'] == 0, (
        'the resumed run re-did completed stages — every re-run would re-spend '
        'search + LLM tokens')


def test_manifest_and_rescan_close_the_crash_loop(tmp_path):
    """The manifest written for a live job is exactly what the rescan needs to
    re-spawn it — the two halves of the crash-resume contract must agree."""
    from lib.production.jobs import resume_running_jobs, write_manifest

    jobs = tmp_path / 'jobs'
    (jobs / 'j1').mkdir(parents=True)
    task = {'task_id': 'j1', 'topic': '天空', 'lang': 'zh', 'workdir': str(jobs / 'j1')}
    assert write_manifest(str(jobs / 'j1'), task,
                          fields=('task_id', 'topic', 'lang', 'workdir'),
                          kind='motion-video', state='running')

    seen = []
    n = resume_running_jobs(str(jobs), is_live=lambda t: False,
                            respawn=lambda t, w, m: seen.append((t, m['topic'])))
    assert n == 1 and seen == [('j1', '天空')]

    # Once the job reports done, the rescan must leave it alone.
    write_manifest(str(jobs / 'j1'), task,
                   fields=('task_id', 'topic', 'lang', 'workdir'),
                   kind='motion-video', state='done')
    assert resume_running_jobs(str(jobs), is_live=lambda t: False,
                               respawn=lambda t, w, m: seen.append(t)) == 0
