"""tests/test_motion_video_p4.py — Topic→video front-half (P4) unit suite.

Covers docs/PRODUCTION_PIPELINE_DESIGN.md P4 (owner-ratified 2026-07-25):

  * stage-graph contract (:mod:`lib.production.stages`, relocated there from
    ``lib.motion_video._stages`` in P6): checkpointed resume, retry, gate
    rejection, abort — the crash-resume correctness contract owner made a
    hard requirement.
  * video recipe (:mod:`lib.motion_video._recipe`): research→script→timeline
    with fakes; the fact-discipline gate (拍板 #4); real-TTS-duration timeline
    vs char-estimate fallback; scene-count cost cap (拍板 #3).
  * produce_video tool registration is NOT project-gated (拍板 #2) and IS
    search-gated.

All seams are monkeypatched — no network / LLM / TTS / render.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════
#  Stage-graph contract (relocated to lib.production.stages in P6)
# ══════════════════════════════════════════════════════════

# NOTE: import the REAL home, not the lib.motion_video._stages back-compat
# shim. run_stages resolves stage_is_done as a module global of its OWN
# module, so the NEUTER below must patch it there — patching the shim would
# silently no-op and the neuter would stop biting.
from lib.production import stages as st


def _stage(name, run, **kw):
    return st.Stage(name, run, **kw)


def test_stages_run_in_order_and_checkpoint(tmp_path):
    calls = []
    stages = [
        _stage('a', lambda ctx: calls.append('a') or {'v': 1}),
        _stage('b', lambda ctx: calls.append('b') or {'v': 2}),
    ]
    state_path = str(tmp_path / 'state.json')
    arts = st.run_stages(stages, {}, state_path=state_path)
    assert calls == ['a', 'b']
    assert arts['a']['v'] == 1 and arts['b']['v'] == 2
    # State file records BOTH as done (the checkpoint).
    state = st.load_state(state_path)
    assert st.stage_is_done(state, 'a') and st.stage_is_done(state, 'b')


def test_stages_resume_skips_completed(tmp_path):
    """A completed stage recorded in the state file is NOT re-run — this is
    the crash-resume correctness contract."""
    calls = []
    state_path = str(tmp_path / 'state.json')
    # First run: only 'a' completes, 'b' crashes.
    def crash(ctx):
        calls.append('b1')
        raise RuntimeError('boom')
    with pytest.raises(st.StageFailed):
        st.run_stages([_stage('a', lambda c: calls.append('a1') or {'x': 1}),
                       _stage('b', crash)],
                      {}, state_path=state_path)
    assert calls == ['a1', 'b1']
    # Second run (resume): 'a' is skipped, 'b' now succeeds.
    calls.clear()
    st.run_stages([_stage('a', lambda c: calls.append('a2') or {'x': 9}),
                   _stage('b', lambda c: calls.append('b2') or {'y': 2})],
                  {}, state_path=state_path)
    assert 'a2' not in calls  # skipped from checkpoint
    assert calls == ['b2']


def test_stages_neuter_resume_proves_loadbearing(tmp_path, monkeypatch):
    """NEUTER: force stage_is_done to always be False → a 'completed' stage
    re-runs, proving the resume-skip is load-bearing."""
    state_path = str(tmp_path / 'state.json')
    st.run_stages([_stage('a', lambda c: {'x': 1})], {}, state_path=state_path)
    calls = []
    monkeypatch.setattr(st, 'stage_is_done', lambda state, name: False)
    st.run_stages([_stage('a', lambda c: calls.append('a') or {'x': 2})],
                  {}, state_path=state_path)
    assert calls == ['a']  # re-ran because the skip gate was neutered


def test_stages_gate_retry_then_fail(tmp_path):
    attempts = {'n': 0}
    def flaky(ctx):
        attempts['n'] += 1
        return {'ok_val': attempts['n']}
    # Gate passes only when ok_val >= 2.
    stage = _stage('g', flaky,
                   gate=lambda ctx, art: [] if art['ok_val'] >= 2 else ['too low'],
                   retry=2)
    arts = st.run_stages([stage], {}, state_path=str(tmp_path / 's.json'))
    assert arts['g']['ok_val'] == 2  # first attempt failed the gate, second passed

    # With no retries the same gate fails hard.
    attempts['n'] = 0
    with pytest.raises(st.StageFailed) as ei:
        st.run_stages([_stage('g', flaky,
                              gate=lambda ctx, art: ['always'], retry=0)],
                      {}, state_path=str(tmp_path / 's2.json'))
    assert ei.value.stage == 'g'


def test_stages_abort_between(tmp_path):
    flag = {'v': False}
    def a(ctx):
        flag['v'] = True  # trip abort after stage a
        return {}
    with pytest.raises(st.StageAborted):
        st.run_stages([_stage('a', a), _stage('b', lambda c: {})],
                      {}, state_path=str(tmp_path / 's.json'),
                      abort_check=lambda: flag['v'])


# ══════════════════════════════════════════════════════════
#  Video recipe (_recipe)
# ══════════════════════════════════════════════════════════

from lib.motion_video import _recipe as rec


_FAKE_RESULTS = [
    {'title': 'Why the sky is blue', 'url': 'https://example.com/rayleigh',
     'snippet': 'Rayleigh scattering makes shorter blue wavelengths scatter more.'},
    {'title': 'Atmosphere', 'url': 'https://sci.example.org/atmo',
     'snippet': 'Air molecules scatter sunlight; blue dominates the daytime sky.'},
]


def _patch_research(monkeypatch, results=_FAKE_RESULTS):
    monkeypatch.setattr(rec, '_web_search', lambda q, user_question='',
                        freshness='': list(results))


def _patch_script(monkeypatch, segments=None):
    segs = segments or ['天空是蓝色的,因为空气分子散射阳光。',
                        '蓝光波长短,被散射得更多,所以我们看到蓝天。']
    payload = json.dumps({'title': '天空为什么是蓝色', 'segments': segs},
                         ensure_ascii=False)
    monkeypatch.setattr(rec, '_llm_chat',
                        lambda messages, **kw: (payload, {'prompt_tokens': 10,
                                                          'completion_tokens': 20}))


def test_research_gate_rejects_no_sourced_cards(monkeypatch):
    # All results carry NO url → zero cards → gate fails (fact discipline).
    _patch_research(monkeypatch, results=[{'title': 'x', 'snippet': 'no link'}])
    errors = rec._gate_research({}, rec._run_research(
        {'topic': 't', 'lang': 'zh'}))
    assert errors and 'grounded' in errors[0]


def test_research_extracts_sourced_cards(monkeypatch):
    _patch_research(monkeypatch)
    art = rec._run_research({'topic': '天空为什么蓝', 'lang': 'zh'})
    assert art['cards']
    assert all(c['url'].startswith('https://') for c in art['cards'])
    assert rec._gate_research({}, art) == []


def test_script_appends_sources_card(monkeypatch):
    _patch_script(monkeypatch)
    ctx = {'topic': 't', 'lang': 'zh', 'max_scenes': 8,
           'artifacts': {'research': {'cards': _FAKE_RESULTS[:]}}}
    # normalize research cards to fact-card shape first
    ctx['artifacts']['research']['cards'] = rec._cards_from_results(_FAKE_RESULTS)
    art = rec._run_script(ctx)
    # 拍板 #4 unchanged: the run always carries a sources credit — but since
    # the owner (2026-07-26) ruled it a SILENT visual card, it rides the
    # artifact as sources_line, NOT as a narration segment (which TTS would
    # voice). The timeline stage turns it into the final spoken=False scene.
    assert art['sources_line'].startswith('资料来源')
    assert not any('资料来源' in s for s in art['segments'])
    assert rec._gate_script({}, art) == []


def test_script_respects_max_scenes_cap(monkeypatch):
    # LLM returns 20 segments; max_scenes=5 → clamp to 4 narration segments.
    # (The sources end card is added by the TIMELINE stage as spoken=False,
    # no longer counted in segments — owner 2026-07-26 silent-card contract.)
    _patch_script(monkeypatch, segments=[f'第{i}段' for i in range(20)])
    ctx = {'topic': 't', 'lang': 'zh', 'max_scenes': 5,
           'artifacts': {'research': {'cards': rec._cards_from_results(_FAKE_RESULTS)}}}
    art = rec._run_script(ctx)
    assert len(art['segments']) == 4  # cost cap, 拍板 #3


def test_topic_script_is_locked_to_requested_model(monkeypatch):
    seen = {}

    def fake_chat(messages, **kwargs):
        seen.update(kwargs)
        return ('{"title":"澎程","segments":["第一幕。","第二幕。",'
                '"第三幕。"]}', {})

    monkeypatch.setattr(rec, '_llm_chat', fake_chat)
    ctx = {'topic': '小米澎程', 'lang': 'zh', 'max_scenes': 8,
           'model': 'kimi-k3',
           'artifacts': {'research': {
               'cards': rec._cards_from_results(_FAKE_RESULTS)}}}
    rec._run_script(ctx)
    assert seen['prefer_model'] == 'kimi-k3'


def test_topic_script_carries_art_direction_into_scenes(monkeypatch):
    reply = {
        'title': '澎程',
        'beats': [
            {'text': '远方，从容展开。', 'on_screen': '空间，自由生长',
             'visual': 'Wide road reveal with layered parallax',
             'source_ids': ['S1'],
             'assets': [{'role': 'subject',
                         'semantic_target': 'the vehicle on the open road',
                         'prompt': 'premium SUV at sunrise, no text'}]},
            {'text': '智能，让旅途彼此连接。', 'on_screen': '人车家全生态',
             'visual': 'Connected nodes orbit the cabin',
             'source_ids': ['S2'],
             'assets': [{'role': 'diagram',
                         'prompt': 'abstract connected ecosystem, no labels'}]},
            {'text': '每一次出发，都通向更大的世界。', 'on_screen': '向远方',
             'visual': 'Horizon resolve', 'source_ids': ['S1'], 'assets': []},
        ],
    }
    monkeypatch.setattr(
        rec, '_llm_chat',
        lambda *a, **k: (json.dumps(reply, ensure_ascii=False), {}))
    ctx = {'topic': '小米澎程', 'lang': 'zh', 'max_scenes': 8,
           'model': 'kimi-k3',
           'artifacts': {'research': {
               'cards': rec._cards_from_results(_FAKE_RESULTS)}}}
    script = rec._run_script(ctx)
    scenes = rec._provisional_scenes(script['segments'], '', script['beats'])
    assert scenes[0]['on_screen'] == '空间，自由生长'
    assert scenes[0]['source_ids'] == ['S1']
    assert scenes[0]['assets'][0]['role'] == 'subject'
    assert scenes[0]['assets'][0]['semantic_target'] == (
        'the vehicle on the open road')
    assert scenes[1]['visual'] == 'Connected nodes orbit the cabin'


def test_topic_script_gate_rejects_stale_current_facts_and_feeds_retry():
    card = {
        'id': 'S1', 'title': '官方预售',
        'point': 'N70 Max 预售价 25.99 万元，现已开启预售。',
        'url': 'https://example.com/latest', 'host': 'example.com',
        'query_lane': 'current', 'query_lanes': ['current'],
    }
    artifact = {
        'segments': ['新车即将到来。', '价格尚未公布。'],
        'beats': [], 'source_ids': [],
    }
    ctx = {'artifacts': {'research': {'cards': [card]}}}
    errors = rec._gate_script(ctx, artifact)
    assert any('ignores every current-state source' in error
               for error in errors)
    assert any('announced presale price' in error for error in errors)
    assert ctx['_script_gate_feedback'] == errors


def test_timeline_uses_real_tts_durations(monkeypatch, tmp_path):
    """The timeline must be measured from real TTS audio, not char-estimated
    (owner requirement: delete the 4.2 chars/s hard estimate)."""
    segs = ['第一段口播', '第二段口播', '资料来源:example.com']
    manifest = {'ok': True, 'degraded': False, 'scenes': [
        {'scene_id': 'scene-001', 'target_duration': 4.0, 'audio_duration': 3.7,
         'srt_duration': 4.0, 'overflow': 0.0, 'wav': str(tmp_path / 'a.wav')},
        {'scene_id': 'scene-002', 'target_duration': 6.0, 'audio_duration': 5.6,
         'srt_duration': 6.0, 'overflow': 0.0, 'wav': str(tmp_path / 'b.wav')},
        {'scene_id': 'scene-003', 'target_duration': 3.0, 'audio_duration': 2.5,
         'srt_duration': 3.0, 'overflow': 0.0, 'wav': str(tmp_path / 'c.wav')},
    ]}
    monkeypatch.setattr(rec, '_tts_durations',
                        lambda scenes, out_dir, **kw: manifest)
    ctx = {'topic': 't', 'lang': 'zh', 'workdir': str(tmp_path),
           'narration': True, 'alignment': 'loose',
           'artifacts': {'script': {'segments': segs}}}
    art = rec._run_timeline(ctx)
    assert art['timed_from_audio'] is True
    with open(art['scenes_path'], encoding='utf-8') as f:
        scenes = json.load(f)
    # Durations came straight from the manifest (4/6/3 = 13s span).
    assert scenes[0]['end'] - scenes[0]['start'] == pytest.approx(4.0)
    assert scenes[-1]['end'] == pytest.approx(13.0)
    assert scenes[0]['shot_recipe']
    assert scenes[0]['shot_contract_version'] == 'motion-shot-v1'
    assert len(scenes[0]['qa_progresses']) == 4
    assert scenes[-1]['narrative_role'] == 'cta'
    assert rec._gate_timeline({}, art) == []


def test_timeline_falls_back_when_tts_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(rec, '_tts_durations',
                        lambda scenes, out_dir, **kw: {'ok': False, 'degraded': True})
    ctx = {'topic': 't', 'lang': 'zh', 'workdir': str(tmp_path),
           'narration': True, 'artifacts': {'script': {'segments': ['甲乙丙丁' * 4, '第二段']}}}
    art = rec._run_timeline(ctx)
    assert art['timed_from_audio'] is False
    assert os.path.isfile(art['scenes_path'])  # still ships (silent path)
    assert rec._gate_timeline({}, art) == []


def test_build_scenes_from_topic_end_to_end(monkeypatch, tmp_path):
    _patch_research(monkeypatch)
    _patch_script(monkeypatch)
    monkeypatch.setattr(rec, '_tts_durations',
                        lambda scenes, out_dir, **kw: {'ok': False, 'degraded': True})
    out = rec.build_scenes_from_topic('天空为什么是蓝色', str(tmp_path),
                                      lang='zh', narration=True)
    assert out['scenes'] >= 2
    assert os.path.isfile(out['scenes_path'])
    # Checkpoint file exists and records all three stages.
    state = st.load_state(os.path.join(str(tmp_path), 'pipeline_state.json'))
    for name in ('research', 'script', 'timeline'):
        assert st.stage_is_done(state, name), name


# ══════════════════════════════════════════════════════════
#  produce_video tool registration (拍板 #2 / #5)
# ══════════════════════════════════════════════════════════

def _ctx(*, project, search):
    from lib.tools.registry import ToolContext
    return ToolContext(
        cfg={}, task_id='t', project_path='/tmp/x' if project else '',
        project_enabled=project, search_mode='multi' if search else 'off',
        search_enabled=search, fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False, swarm_enabled=False)


def test_produce_video_not_project_gated():
    """拍板 #2: produce_video is available WITHOUT an attached project."""
    from lib.tools.registry import assemble_tool_list
    ctx = _ctx(project=False, search=True)
    assemble_tool_list(ctx)
    names = {t['function']['name'] for t in ctx.enabled_tool_catalog}
    assert 'produce_video' in names
    # ...while the low-level motion_video_* family stays project-gated.
    assert not any(n.startswith('motion_video') for n in names)


def test_produce_video_search_gated():
    from lib.tools.registry import assemble_tool_list
    tools, _ = assemble_tool_list(_ctx(project=False, search=False))
    names = {t['function']['name'] for t in tools}
    assert 'produce_video' not in names  # no research → no grounded facts


# ══════════════════════════════════════════════════════════
#  produce_research reachability (R4 wiring)
#
#  The R4 capability shipped as an ISLAND: recipe + engine + runtime all
#  existed and passed their unit tests, but nothing could reach them — no
#  tool schema, no dispatch handler, no runtime discovery, no crash-resume
#  call. These four tests pin each seam so a future refactor that drops one
#  fails loudly instead of silently re-orphaning the feature.
# ══════════════════════════════════════════════════════════

def test_produce_research_reachable_and_search_gated():
    """SEAM 1+2: the schema reaches the assembled tool list, un-project-gated
    (same posture as produce_video/report) but search-gated — the ideation
    screen is only meaningful against a real harvested corpus."""
    from lib.tools.registry import assemble_tool_list
    ctx = _ctx(project=False, search=True)
    assemble_tool_list(ctx)
    names = [t['function']['name'] for t in ctx.enabled_tool_catalog]
    assert 'produce_research' in names
    # Appended AFTER the existing pair so the cache-stable prefix is untouched.
    assert ([n for n in names if n.startswith('produce_')]
            == ['produce_video', 'produce_report', 'produce_research',
                'produce_slides'])
    off_ctx = _ctx(project=False, search=False)
    off, _ = assemble_tool_list(off_ctx)
    assert 'produce_research' not in {t['function']['name'] for t in off}
    assert 'produce_research' in {
        t['function']['name'] for t in off_ctx.enabled_tool_catalog
    }


def test_produce_research_handler_registered():
    """SEAM 3: a schema with no handler is a phantom tool — the model would
    call it and get 'unknown tool'. Pin the dispatch registration."""
    from lib.tasks_pkg.executor import tool_registry
    import lib.tasks_pkg.handlers  # noqa: F401 — fires the decorators
    assert tool_registry.lookup('produce_research') is not None


def test_produce_research_runtime_discovered_by_generic_tasks():
    """SEAM 4: poll/abort ride /api/v1/tasks/*, which discovers runtimes by
    their own .kind. Without this the job runs but is unpollable.

    Asserts the runtime shape ``_registries()`` requires, rather than importing
    routes (whose package init needs the full Quart app)."""
    mod = __import__('lib.research.runtime', fromlist=['_research_runtime'])
    rt = getattr(mod, '_research_runtime', None)
    assert rt is not None and rt.kind == 'research'
    assert all(hasattr(rt, a) for a in ('_lock', '_tasks', 'get', 'poll', 'abort'))
    # The registry entry itself must name this module/attr pair.
    import inspect
    from routes.api_v1 import tasks as tasks_mod
    assert "'lib.research.runtime', '_research_runtime'" in \
        inspect.getsource(tasks_mod._registries)


def test_produce_research_crash_resume_wired():
    """SEAM 5: the resume sweep must be CALLED at startup, not merely defined
    — otherwise a crashed job's harvested corpus is stranded on disk."""
    import inspect
    from lib.server_background_services import start_background_services
    src = inspect.getsource(start_background_services)
    assert 'resume_interrupted_research' in src


def test_produce_research_handler_clamps_and_threads_args(monkeypatch):
    """The handler is the arg-validation boundary: clamp n_ideas, require a
    direction, and thread conv_id through so the job can post back."""
    import lib.tasks_pkg.handlers.motion_video as hdl
    import lib.research as research_pkg

    captured = {}

    def fake_produce(direction, **kw):
        captured['direction'] = direction
        captured.update(kw)
        return {'task_id': 'research_fake1', 'deduped': False}

    monkeypatch.setattr(research_pkg, 'produce_research', fake_produce)
    monkeypatch.setattr(hdl, '_build_simple_meta', lambda *a, **k: {})
    monkeypatch.setattr(hdl, '_finalize_tool_round', lambda *a, **k: None)

    task = {'events': [], 'conv_id': 'convX'}
    _, content, _ = hdl._handle_produce_research(
        task, {'id': 'tc1'}, 'produce_research', 'tc1',
        {'direction': 'KV cache compression', 'lang': 'zh', 'n_ideas': 99,
         'seed_arxiv_ids': ['2312.00752']},
        0, {'tool_calls': []}, cfg=None, project_path='', project_enabled=False)
    result = json.loads(content)
    assert result['ok'] and result['task_id'] == 'research_fake1'
    assert result['poll'] == '/api/v1/tasks/research_fake1'
    assert result['n_ideas'] == 12          # clamped from 99
    assert captured['conv_id'] == 'convX'
    assert captured['seed_arxiv_ids'] == ['2312.00752']

    # An empty direction is rejected before any job is spawned.
    _, content2, _ = hdl._handle_produce_research(
        task, {'id': 't2'}, 'produce_research', 't2', {}, 0,
        {'tool_calls': []}, cfg=None, project_path='', project_enabled=False)
    assert json.loads(content2)['ok'] is False


# ══════════════════════════════════════════════════════════
#  engine job-manifest crash-resume helpers
# ══════════════════════════════════════════════════════════

from lib.motion_video import engine as eng


def test_write_job_manifest_roundtrip(tmp_path):
    task = {'task_id': 'motion_x', 'workdir': str(tmp_path), 'topic': 'sky',
            'lang': 'zh', 'narration': True, 'width': 1080, 'height': 1440}
    eng.write_job_manifest(task, kind='topic', state='running')
    from lib.json_store import read_json
    m = read_json(os.path.join(str(tmp_path), 'job.json'))
    assert m['state'] == 'running' and m['kind'] == 'topic'
    assert m['topic'] == 'sky' and m['task_id'] == 'motion_x'


def test_resume_interrupted_jobs_respawns_running(monkeypatch, tmp_path):
    """A job.json in the 'running' state re-spawns on startup; done/error do not."""
    jobs = tmp_path / 'jobs'
    for jid, state in (('run1', 'running'), ('done1', 'done'), ('err1', 'error')):
        d = jobs / jid
        d.mkdir(parents=True)
        (d / 'job.json').write_text(json.dumps({
            'task_id': jid, 'state': state, 'kind': 'topic', 'workdir': str(d),
            'topic': 't', 'width': 1080, 'height': 1440, 'narration': True}))
    monkeypatch.setattr('lib.motion_video._env.motion_root', lambda: str(tmp_path))
    spawned = []
    from lib.motion_video.runtime import _motion_runtime
    monkeypatch.setattr(_motion_runtime, 'spawn',
                        lambda tid, fn, task: spawned.append(tid))
    monkeypatch.setattr(_motion_runtime, 'get', lambda tid: None)
    n = eng.resume_interrupted_jobs()
    assert n == 1
    assert spawned == ['run1']


def test_reusable_manifest_matches_scenes(tmp_path):
    audio = tmp_path / 'audio'
    audio.mkdir()
    wav = audio / 'scene-001.wav'
    wav.write_bytes(b'RIFF')
    from lib.json_store import write_json_atomic
    write_json_atomic(str(audio / 'manifest.json'), {'ok': True, 'scenes': [
        {'scene_id': 'scene-001', 'wav': str(wav), 'audio_duration': 3.0,
         'target_duration': 3.0, 'overflow': 0.0}]})
    scenes = [{'id': 'scene-001'}]
    assert eng._reusable_manifest(str(audio), scenes) is not None
    # A missing wav → not reusable.
    wav.unlink()
    assert eng._reusable_manifest(str(audio), scenes) is None
