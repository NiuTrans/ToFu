"""tests/test_production_runtime.py — ProductionRuntime + job manifest (P6).

The shared lifecycle described in docs/modules/production.md was extracted
after the per-capability ``runtime.py`` implementations proved the same shape:
67% byte-identical across three samples, and the job-manifest / crash-resume
pair was the same shape in two. Both now live in ``lib/production/``.

What these tests pin:
  * the substrate behaviours themselves (dedup liveness + pruning, terminal
    retention, manifest round-trip, resume idempotence);
  * that all production capabilities actually ride it — a capability that quietly
    re-hand-rolls its own copy is the regression this epic exists to prevent;
  * that capability facades do not expose TaskRuntime's registry or lock.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

from lib.production.jobs import (MANIFEST_NAME, read_manifest,
                                 resume_running_jobs, write_manifest)
from lib.production.runtime import ProductionRuntime


def _rt(kind='test-kind', prefix='tk', ttl=3600):
    return ProductionRuntime(kind, id_prefix=prefix, ttl=ttl,
                             push_channel=None, log_label='Test')


# ══════════════════════════════════════════════════════════
#  ProductionRuntime
# ══════════════════════════════════════════════════════════

def test_task_id_uses_prefix():
    r = _rt(prefix='motion')
    tid = r.new_task_id()
    assert tid.startswith('motion_') and len(tid) == len('motion_') + 16


def test_create_task_sets_shape_and_custom_fields():
    r = _rt()
    t = r.create_task('t1', meta={'a': 1}, fields={'topic': 'sky', 'w': 2}, user_id=TEST_OWNER_USER_ID)
    assert t['task_id'] == 't1' and t['status'] == 'pending'
    assert t['result'] is None and t['updated_at'] > 0
    assert t['meta'] == {'a': 1, 'userId': TEST_OWNER_USER_ID}
    assert t['user_id'] == TEST_OWNER_USER_ID
    assert t['topic'] == 'sky' and t['w'] == 2
    assert r.get('t1') is t           # registered in the shared registry
    assert t['kind'] == r.kind


def test_dedup_index_returns_live_task_only():
    r = _rt()
    t = r.create_task('t1', user_id=TEST_OWNER_USER_ID)
    r.index_register(('k',), 't1')
    assert r.index_get(('k',)) == 't1'          # pending counts as live
    r.runtime.mark_running(t['id'])
    assert r.index_get(('k',)) == 't1'
    r.finish(t['id'], result='done')
    assert r.index_get(('k',)) is None          # terminal → pruned
    assert r.dedup_stats()['size'] == 0


def test_dedup_index_prunes_vanished_task():
    r = _rt()
    r.create_task('t1', user_id=TEST_OWNER_USER_ID)
    r.index_register(('k',), 't1')
    r.runtime.remove_owned('t1', user_id=TEST_OWNER_USER_ID)
    assert r.index_get(('k',)) is None
    assert r.dedup_stats()['size'] == 0


def test_claim_task_is_atomic_under_concurrent_identical_starts():
    """The old index_get→create→register sequence was a race: four identical
    research starts produced four live jobs and raced their shared corpus."""
    r = _rt()

    def claim(i):
        task, existing = r.claim_task(
            ('same',), f't{i}',
            user_id=TEST_OWNER_USER_ID,
            fields={'i': i},
        )
        return task, existing

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(24)))
    winners = [task for task, existing in results if task is not None]
    joined = [existing for task, existing in results if existing is not None]
    assert len(winners) == 1
    assert len(joined) == 23
    winner_id = winners[0]['task_id']
    assert set(joined) == {winner_id}
    assert r.runtime.task_ids() == frozenset({winner_id})


def test_append_event_touches_updated_at():
    r = _rt()
    t = r.create_task('t1', user_id=TEST_OWNER_USER_ID)
    t['updated_at'] = 0.0
    seq = r.append_event(t, {'type': 'phase'})
    assert seq == 0 and t['updated_at'] > 0


def test_cleanup_stale_uses_runtime_terminal_clock_and_prunes_index(monkeypatch):
    r = _rt(ttl=10)
    now = [100.0]
    monkeypatch.setattr('lib.agent_core.task_runtime.time.time', lambda: now[0])
    live = r.create_task('live', user_id=TEST_OWNER_USER_ID)
    old = r.create_task('old', user_id=TEST_OWNER_USER_ID)
    r.index_register(('k',), 'old')
    r.finish(old['id'], result='done')
    now[0] += 11
    assert r.cleanup_stale() == 1
    assert r.get('old') is None and r.get('live') is live
    assert r.dedup_stats()['size'] == 0
    assert r.dedup_stats()['evictions']['ttl'] == 1


def test_cleanup_keeps_recent_terminal_task():
    r = _rt(ttl=3600)
    t = r.create_task('t1', user_id=TEST_OWNER_USER_ID)
    r.finish(t['id'], result='done')
    assert r.cleanup_stale() == 0
    assert r.get('t1') is t


def test_dedup_retention_never_drops_active_authority():
    """Capacity pressure is visible, but cannot launch duplicate live work."""
    r = ProductionRuntime(
        'dedup-bounded', id_prefix='db', max_tasks=4, max_dedup_keys=2)
    for index in range(3):
        task = r.create_task(f't{index}', user_id=TEST_OWNER_USER_ID)
        r.runtime.mark_running(task['id'])
        r.index_register((index,), task['id'])

    stats = r.dedup_stats()
    assert stats['size'] == 3
    assert stats['capacity'] == 2
    assert stats['over_capacity'] == 1
    assert all(r.index_get((index,)) == f't{index}' for index in range(3))


def test_dedup_retention_prunes_terminal_and_orphan_keys_with_counts():
    r = ProductionRuntime(
        'dedup-prune', id_prefix='dp', max_tasks=4, max_dedup_keys=2)
    terminal = r.create_task('terminal', user_id=TEST_OWNER_USER_ID)
    r.index_register(('terminal',), terminal['id'])
    r.finish(terminal['id'], result='done')
    assert r.index_get(('terminal',)) is None

    live = r.create_task('live', user_id=TEST_OWNER_USER_ID)
    r.index_register(('live',), live['id'])
    r.runtime.remove_owned(live['id'], user_id=TEST_OWNER_USER_ID)
    r.index_register(('new',), 'not-yet-created')

    stats = r.dedup_stats()
    assert stats['size'] == 0
    assert stats['evictions']['terminal'] == 1
    assert stats['evictions']['orphan'] == 2


# ══════════════════════════════════════════════════════════
#  Job manifest + crash-resume rescan
# ══════════════════════════════════════════════════════════

def test_manifest_round_trip_and_none_skipping(tmp_path):
    task = {'task_id': 'j1', 'topic': 'sky', 'lang': 'zh', 'absent': None}
    assert write_manifest(str(tmp_path), task,
                          fields=('task_id', 'topic', 'lang', 'absent'),
                          kind='motion-video', state='running')
    m = read_manifest(str(tmp_path))
    assert m['task_id'] == 'j1' and m['topic'] == 'sky'
    assert m['kind'] == 'motion-video' and m['state'] == 'running'
    # A None field is omitted, so resume doesn't overwrite a default with null.
    assert 'absent' not in m


def test_manifest_missing_workdir_is_noop():
    assert write_manifest('', {'task_id': 'x'}, fields=('task_id',),
                          kind='k', state='running') is False


def test_read_manifest_absent_or_malformed(tmp_path):
    assert read_manifest(str(tmp_path)) is None
    (tmp_path / MANIFEST_NAME).write_text('not json{')
    assert read_manifest(str(tmp_path)) is None


def _seed(jobs, jid, state):
    d = jobs / jid
    d.mkdir(parents=True)
    (d / MANIFEST_NAME).write_text(json.dumps(
        {'task_id': jid, 'state': state, 'kind': 'k'}))
    return d


def test_resume_only_respawns_running(tmp_path):
    jobs = tmp_path / 'jobs'
    for jid, state in (('r1', 'running'), ('d1', 'done'), ('e1', 'error')):
        _seed(jobs, jid, state)
    spawned = []
    n = resume_running_jobs(str(jobs), is_live=lambda t: False,
                            respawn=lambda t, w, m: spawned.append(t))
    assert n == 1 and spawned == ['r1']


def test_resume_is_idempotent_via_is_live(tmp_path):
    """A job already in the registry must not be double-spawned."""
    jobs = tmp_path / 'jobs'
    _seed(jobs, 'r1', 'running')
    spawned = []
    n = resume_running_jobs(str(jobs), is_live=lambda t: True,
                            respawn=lambda t, w, m: spawned.append(t))
    assert n == 0 and spawned == []


def test_resume_survives_one_bad_job(tmp_path):
    """One job that fails to re-spawn must not stop the others."""
    jobs = tmp_path / 'jobs'
    _seed(jobs, 'bad', 'running')
    _seed(jobs, 'good', 'running')

    def respawn(tid, wd, m):
        if tid == 'bad':
            raise RuntimeError('cannot rebuild')

    n = resume_running_jobs(str(jobs), is_live=lambda t: False,
                            respawn=respawn)
    assert n == 1          # 'good' still resumed


def test_resume_missing_dir_is_zero(tmp_path):
    assert resume_running_jobs(str(tmp_path / 'nope'),
                               is_live=lambda t: False,
                               respawn=lambda *a: None) == 0


# ══════════════════════════════════════════════════════════
#  Capability boundaries
# ══════════════════════════════════════════════════════════

_CAPS = (
    ('lib.motion_video.runtime', '_motion_runtime', 'motion-video'),
    ('lib.paper.podcast_runtime', '_podcast_runtime', 'paper-podcast'),
    ('lib.longform.runtime', '_longform_runtime', 'longform-report'),
)


@pytest.mark.parametrize('mod_path,attr,kind', _CAPS)
def test_capability_rides_production_runtime(mod_path, attr, kind):
    """Each capability must expose a ProductionRuntime — if one re-hand-rolls
    its own dedup/lifecycle copy, this epic's whole point is lost."""
    import importlib
    mod = importlib.import_module(mod_path)
    prod = getattr(mod, '_production')
    assert isinstance(prod, ProductionRuntime)
    assert prod.kind == kind
    assert getattr(mod, attr) is prod.runtime


@pytest.mark.parametrize('mod_path,attr,kind', _CAPS)
def test_capability_facades_do_not_expose_runtime_storage(mod_path, attr, kind):
    import importlib
    mod = importlib.import_module(mod_path)
    prefix = {'motion-video': '_motion', 'paper-podcast': '_podcast',
              'longform-report': '_longform'}[kind]
    assert not hasattr(mod, f'{prefix}_tasks')
    assert not hasattr(mod, f'{prefix}_tasks_lock')
    assert not hasattr(mod, f'{prefix}_dedup_index')


def test_substrate_stays_capability_agnostic():
    """lib/production/ must not import any capability — otherwise the
    'horizontal layer' claim is false and recipe #4 inherits the baggage."""
    import ast
    import lib.production.jobs as jobs_mod
    import lib.production.runtime as rt_mod
    for mod in (rt_mod, jobs_mod):
        tree = ast.parse(open(mod.__file__, encoding='utf-8').read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            for banned in ('motion_video', 'longform', 'paper', 'tts'):
                assert banned not in name, f'{mod.__name__} imports {name!r}'
