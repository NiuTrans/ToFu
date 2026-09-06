"""The PRODUCT-QUALITY axis: a degraded job must be visible over the API.

Background (epic pt_4ea121206f1a4f46). A research pass whose structural gate
wiped 100% of the ideas wrote ``state='degraded'`` into job.json, set
``result.degraded=True`` and carried ``degraded`` on the SSE final frame — but
``/api/v1/tasks/<id>`` still reported ``status='done'``, so any client that
reads status alone saw an unqualified success.

The fix is deliberately NOT a new ``status`` member (that would change the
meaning of every ``status in (...)`` terminal check, and every future quality
dimension would need another member). ``status`` stays the LIFECYCLE axis;
``artifact_quality`` is a separate PRODUCT axis. (The field is not called
``quality`` because motion-video already stores its render preset under that
key on the same task dict — two meanings of one word must not share a key.)

These are BEHAVIOUR guards per the charter's "assert the result, not the
implementation" rule: they drive the real ``finish()`` and read the real HTTP
response, so the contract keeps biting if the internals are rewritten.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.agent_core.task_runtime import TaskRuntime  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── The substrate: status and quality are independent axes ──

def test_degraded_job_keeps_lifecycle_status_done():
    """The whole point: quality must NOT leak into the lifecycle axis."""
    rt = TaskRuntime('quality-test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], result={'accepted': []}, degraded=True,
              degraded_reason='structural gate rejected every idea')

    assert task['status'] == 'done', (
        'degraded polluted the lifecycle axis — every terminal check '
        "(status in ('done','error','aborted')) now means something different")
    assert task['artifact_quality'] == {
        'degraded': True,
        'reason': 'structural gate rejected every idea'}


def test_clean_job_is_assessed_and_healthy():
    rt = TaskRuntime('quality-test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], result={'ok': 1}, degraded=False)
    assert task['status'] == 'done'
    assert task['artifact_quality'] == {'degraded': False, 'reason': ''}


def test_unassessed_job_reports_no_quality_verdict():
    """Tri-state: None means nobody looked — NOT the same as 'clean'.

    Chat/translate never assess artifact quality; they must not be made to
    look explicitly-healthy by omission.
    """
    rt = TaskRuntime('quality-test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], result={'ok': 1})
    assert task['status'] == 'done'
    assert task['artifact_quality'] is None
    poll = rt.poll(task['id'])
    assert 'artifact_quality' not in poll, (
        'an unassessed task advertised a quality verdict it never made')


def test_terminal_event_and_poll_carry_the_verdict():
    """A live SSE/WS subscriber must learn the verdict without a second GET."""
    rt = TaskRuntime('quality-test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], result={'x': 1}, degraded=True,
              degraded_reason='narration degraded to silent')

    final = task['events'][-1]
    assert final['type'] == 'done'
    assert final['status'] == 'done'
    assert final['artifact_quality']['degraded'] is True

    poll = rt.poll(task['id'])
    assert poll['status'] == 'done'
    assert poll['done'] is True
    assert poll['artifact_quality']['degraded'] is True
    assert 'narration degraded' in poll['artifact_quality']['reason']


def test_error_path_still_wins_over_quality():
    """A real failure is an ERROR, not a degraded success."""
    rt = TaskRuntime('quality-test')
    task = rt.create(user_id=1)
    rt.finish(task['id'], error='harvest exploded', degraded=True,
              degraded_reason='should not mask the error')
    assert task['status'] == 'error'
    assert task['error'] is not None
    assert task['artifact_quality']['degraded'] is True


def test_legacy_task_dict_without_quality_key_still_finishes():
    """Chat and older test code insert their own dicts straight into _tasks."""
    import threading
    rt = TaskRuntime('quality-test')
    legacy = {'id': 'legacy-1', 'status': 'running', 'events': [],
              'events_lock': threading.Lock(),
              'abort_event': threading.Event(),
              'result': None, 'error': None}
    with rt._lock:
        rt._tasks['legacy-1'] = legacy
    assert rt.finish('legacy-1', result={'ok': 1}) is True
    assert legacy['status'] == 'done'


# ── The API surface: the easiest link to forget ──

def _tasks_app(runtime):
    """A minimal Quart app exposing /api/v1/tasks against ONE runtime.

    Auth: an explicit admin ``AuthContext`` is installed per request. We do
    NOT lean on the open-mode loopback grant — the in-process test client
    reports the literal ``'<local>'`` peer, which auto-admins every request
    and would make this file a false-green (pt_f6742ab638114f0f). Granting
    the scope on purpose keeps the test about the RESPONSE SHAPE.
    """
    from quart import Quart, g, request

    import routes.api_v1.tasks as tasks_mod
    from lib.api_keys import local_admin_context
    from routes.api_v1.tasks import api_v1_tasks_bp

    app = Quart(__name__)
    app.config['TESTING'] = True

    @app.before_request
    async def _grant():
        context = local_admin_context()
        context.user_id = request.headers.get('X-Test-User', '1')
        g.auth_ctx = context
        g.rate_decision = None

    app.register_blueprint(api_v1_tasks_bp)
    return app, tasks_mod


@pytest.fixture()
def api(monkeypatch):
    """/api/v1/tasks wired to a private throwaway runtime."""
    rt = TaskRuntime('quality-test')
    app, tasks_mod = _tasks_app(rt)
    monkeypatch.setattr(tasks_mod, '_registries', lambda: {'quality-test': rt})
    return app, rt


def test_degraded_job_is_visible_through_the_task_api(api):
    """★ The epic's headline requirement, end to end.

    NEUTER: drop ``artifact_quality`` from ``_public_task``'s output (e.g. add
    it to SKIP) and this must go red — a degraded job would once again be
    indistinguishable from a clean one over HTTP.
    """
    app, rt = api
    task = rt.create(user_id=1)
    rt.finish(task['id'], result={'accepted': [], 'rejected': []},
              degraded=True,
              degraded_reason='novelty retrieval returned nothing for every idea')

    async def go():
        r = await app.test_client().get(f'/api/v1/tasks/{task["id"]}')
        assert r.status_code == 200
        return await r.get_json()

    body = _run(go())
    assert body['status'] == 'done', 'lifecycle axis changed shape'
    quality = body.get('artifact_quality')
    assert quality is not None, (
        'the API dropped the quality axis — a client reading this response '
        'sees an unqualified success for a job the pipeline knows is sick')
    assert quality['degraded'] is True
    assert 'novelty retrieval' in quality['reason']


def test_running_task_detail_omits_private_set_state(api):
    """Private runtime state must neither leak nor turn polling into HTTP 500."""
    app, rt = api
    task = rt.create(user_id=1)
    task['_budgetWarnings'] = {'max_tokens', 'max_rounds'}
    task['_nestedRuntimeState'] = {
        'excluded_pairs': {('provider-b', 'model-2'), ('provider-a', 'model-1')},
    }

    async def go():
        response = await app.test_client().get(
            f'/api/v1/tasks/{task["id"]}')
        assert response.status_code == 200
        return await response.get_json()

    body = _run(go())
    assert '_budgetWarnings' not in body
    assert '_nestedRuntimeState' not in body
    assert body['id'] == task['id']
    assert body['status'] == 'pending'


def test_task_detail_replaces_event_bodies_with_cursor_summary(api):
    app, rt = api
    task = rt.create(user_id=1)
    for index in range(3):
        rt.append_event(task['id'], {
            'type': 'progress', 'content': 'large payload', 'index': index,
        })

    async def go():
        response = await app.test_client().get(
            f'/api/v1/tasks/{task["id"]}')
        assert response.status_code == 200
        return await response.get_json()

    body = _run(go())
    assert 'events' not in body
    assert body['event_replay'] == {
        'retained_count': 3,
        'base_cursor': 0,
        'next_cursor': 3,
    }


def test_task_events_page_is_bounded_and_terminal_snapshot_is_last(api):
    app, rt = api
    task = rt.create(user_id=1)
    for index in range(129):
        rt.append_event(task['id'], {
            'type': 'progress', 'index': index,
        })
    rt.finish(task['id'], result={'answer': 'complete'})

    async def go():
        client = app.test_client()
        first_response = await client.get(
            f'/api/v1/tasks/{task["id"]}/events?cursor=0')
        assert first_response.status_code == 200
        first = await first_response.get_json()
        final_response = await client.get(
            f'/api/v1/tasks/{task["id"]}/events'
            f'?cursor={first["next_cursor"]}')
        assert final_response.status_code == 200
        return first, await final_response.get_json()

    first, final = _run(go())
    assert len(first['events']) == 128
    assert first['next_cursor'] == 128
    assert first['caught_up'] is False
    assert first['status'] == 'done'
    assert first['done'] is False
    assert 'result' not in first

    assert [event['seq'] for event in final['events']] == [128, 129]
    assert final['next_cursor'] == 130
    assert final['caught_up'] is True
    assert final['done'] is True
    assert final['result'] == {'answer': 'complete'}


def test_task_list_surface_also_shows_the_verdict(api):
    """The list view hand-lists its fields, so it needs its own guard.

    NEUTER: remove ``'artifact_quality': t.get('artifact_quality')`` from
    ``list_tasks`` and this goes red while the detail-view test above stays
    green.
    """
    app, rt = api
    clean = rt.create(user_id=1)
    rt.finish(clean['id'], result={'ok': 1}, degraded=False)
    sick = rt.create(user_id=1)
    rt.finish(sick['id'], result={'ok': 1}, degraded=True,
              degraded_reason='every section came back empty')

    async def go():
        r = await app.test_client().get('/api/v1/tasks?kind=quality-test')
        assert r.status_code == 200
        return await r.get_json()

    body = _run(go())
    by_id = {t['id']: t for t in body['tasks']}
    assert by_id[clean['id']]['artifact_quality']['degraded'] is False
    assert by_id[sick['id']]['artifact_quality']['degraded'] is True, (
        'the list view cannot distinguish a degraded job from a clean one')
    assert by_id[sick['id']]['status'] == 'done'


def test_task_routes_never_expose_or_mutate_another_owner(api):
    app, rt = api
    own = rt.create(user_id=1)
    foreign = rt.create(user_id=2)

    async def go():
        client = app.test_client()
        headers = {'X-Test-User': '1'}
        listing = await client.get('/api/v1/tasks', headers=headers)
        detail = await client.get(
            f'/api/v1/tasks/{foreign["id"]}', headers=headers)
        events = await client.get(
            f'/api/v1/tasks/{foreign["id"]}/events', headers=headers)
        abort = await client.post(
            f'/api/v1/tasks/{foreign["id"]}/abort', headers=headers)
        delete = await client.delete(
            f'/api/v1/tasks/{foreign["id"]}', headers=headers)
        return (
            await listing.get_json(), detail.status_code, events.status_code,
            abort.status_code, delete.status_code,
        )

    body, detail_status, events_status, abort_status, delete_status = _run(go())
    assert [item['id'] for item in body['tasks']] == [own['id']]
    assert {detail_status, events_status, abort_status, delete_status} == {404}
    assert rt.get(foreign['id']) is foreign
    assert not foreign['abort_event'].is_set()


def test_task_delete_cancels_live_dispatch_before_terminal_removal(api):
    app, rt = api
    task = rt.create(user_id=1)
    released = []
    task['_executionSession'].hold_resource(
        'route', lambda _context: released.append('route'))
    assert rt.mark_running(task['id']) is True

    async def go():
        client = app.test_client()
        active = await client.delete(f'/api/v1/tasks/{task["id"]}')
        assert active.status_code == 409
        active_body = await active.get_json()
        assert active_body['error_code'] == 'task_active'
        assert rt.get(task['id']) is task
        assert task['abort_event'].is_set() is True
        assert released == []

        assert rt.finish(task['id']) is True
        terminal = await client.delete(f'/api/v1/tasks/{task["id"]}')
        assert terminal.status_code == 200
        return await terminal.get_json()

    body = _run(go())
    assert body['status'] == 'deleted'
    assert released == ['route']
    assert rt.get(task['id']) is None


def test_task_delete_refuses_terminal_record_with_durability_debt(api):
    app, rt = api
    task = rt.create(user_id=1)
    assert rt.finish(task['id']) is True
    task['_terminalPersistencePending'] = True

    async def go():
        response = await app.test_client().delete(
            f'/api/v1/tasks/{task["id"]}')
        return response.status_code, await response.get_json()

    status, body = _run(go())
    assert status == 409
    assert body['error_code'] == 'task_settling'
    assert rt.get(task['id']) is task


# ── The three consumers wire the same shape ──

def test_research_engine_reports_its_degraded_verdict(monkeypatch, tmp_path):
    """research already produced degraded/gate_reached — it must now reach the
    task. NEUTER: drop degraded= from the engine's finish() call → red."""
    import lib.research.engine as eng
    from lib.research.runtime import _research_runtime

    task = _research_runtime.create(user_id=1, task_id='research-quality-1')
    task.update({'task_id': 'research-quality-1', 'direction': 'x',
                 'workdir': str(tmp_path), 'lang': 'en', 'n_ideas': 2,
                 'user_id': 1})

    monkeypatch.setattr(eng, '_write_manifest', lambda *a, **k: None)
    monkeypatch.setattr(eng, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(
        'lib.research.recipe.build_research_from_direction',
        lambda *a, **k: {'accepted': [], 'rejected': [{'title': 'i'}],
                         'corpus_size': 5, 'gate_reached': 'structural',
                         'degraded': True,
                         'degraded_reason': 'every idea failed the structural gate'})

    eng.run_research_task(task)

    assert task['status'] == 'done'
    assert task['artifact_quality']['degraded'] is True
    assert 'structural gate' in task['artifact_quality']['reason']


def test_research_engine_treats_unconfirmed_publication_as_error(
        monkeypatch, tmp_path):
    """A durable-write failure is not a valid degraded artifact or clean done."""
    import lib.research.engine as eng
    from lib.production.stages import StageFailed
    from lib.research.runtime import _research_runtime

    task_id = 'research-publish-failure-1'
    task = _research_runtime.create(user_id=1, task_id=task_id)
    task.update({'task_id': task_id, 'direction': 'x',
                 'workdir': str(tmp_path), 'lang': 'en', 'n_ideas': 2,
                 'user_id': 1})

    monkeypatch.setattr(eng, '_write_manifest', lambda *a, **k: None)
    monkeypatch.setattr(eng, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(
        'lib.research.recipe.build_research_from_direction',
        lambda *a, **k: (_ for _ in ()).throw(
            StageFailed('publish', 'repository did not confirm write')))

    eng.run_research_task(task)

    assert task['status'] == 'error'
    assert task['result'] is None
    assert task['error']['kind'] == 'generic'
    assert "stage 'publish' failed" in task['error']['detail']
    assert task['artifact_quality'] is None


def test_longform_engine_flags_a_report_missing_sections(monkeypatch, tmp_path):
    """An outline section whose stage produced nothing is silently dropped by
    _run_assemble; the report is valid but incomplete."""
    import lib.longform.engine as eng
    from lib.longform.runtime import _longform_runtime

    task = _longform_runtime.create(user_id=1, task_id='longform-quality-1')
    task.update({'task_id': 'longform-quality-1', 'topic': 't',
                 'workdir': str(tmp_path), 'lang': 'zh', 'depth': 'brief',
                 'conv_id': ''})

    monkeypatch.setattr(eng, '_write_manifest', lambda *a, **k: None)
    monkeypatch.setattr(eng, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(
        'lib.longform.recipe.build_report_from_topic',
        lambda *a, **k: {'path': str(tmp_path / 'r.md'), 'chars': 900,
                         'sections': 3, 'sections_written': 1,
                         'sections_requested': 3, 'sources': 4, 'title': 'T'})

    eng.run_longform_task(task)

    assert task['status'] == 'done'
    assert task['artifact_quality']['degraded'] is True
    assert 'section' in task['artifact_quality']['reason']


def test_longform_complete_report_is_not_flagged(monkeypatch, tmp_path):
    """Discrimination check: the guard above must not fire on a good report."""
    import lib.longform.engine as eng
    from lib.longform.runtime import _longform_runtime

    task = _longform_runtime.create(user_id=1, task_id='longform-quality-2')
    task.update({'task_id': 'longform-quality-2', 'topic': 't',
                 'workdir': str(tmp_path), 'lang': 'zh', 'depth': 'brief',
                 'conv_id': ''})

    monkeypatch.setattr(eng, '_write_manifest', lambda *a, **k: None)
    monkeypatch.setattr(eng, '_emit', lambda *a, **k: None)
    monkeypatch.setattr(
        'lib.longform.recipe.build_report_from_topic',
        lambda *a, **k: {'path': str(tmp_path / 'r.md'), 'chars': 4000,
                         'sections': 3, 'sections_written': 3,
                         'sections_requested': 3, 'sources': 4, 'title': 'T'})

    eng.run_longform_task(task)

    assert task['status'] == 'done'
    assert task['artifact_quality'] == {'degraded': False, 'reason': ''}


def test_motion_video_narration_degrade_is_a_quality_verdict():
    """motion-video's silent fallback ships a playable mp4 from a pipeline
    whose scene durations are char-estimated (the '8 shots all pinned at
    15.0s' shape). It must be reported, not delivered green.

    Asserted at the runtime seam rather than by driving the full render:
    ffmpeg/Chrome are not available in the test env.
    """
    from lib.motion_video.runtime import _motion_runtime

    task = _motion_runtime.create(user_id=1, task_id='motion-quality-1')
    _motion_runtime.finish(
        'motion-quality-1', result={'narrated': False, 'burn_in_auto': True},
        degraded=True,
        degraded_reason='narration requested but no TTS slot was available')

    assert task['status'] == 'done'
    assert task['artifact_quality']['degraded'] is True
    assert 'TTS' in task['artifact_quality']['reason']
