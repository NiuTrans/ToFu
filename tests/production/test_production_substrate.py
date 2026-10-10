"""Executable contract for the shared production substrate.

The package facade and owner modules expose one implementation, the substrate
remains capability-neutral, and stage checkpoints preserve recovery semantics.
"""

from __future__ import annotations

import ast
import os
import threading

import pytest

pytestmark = pytest.mark.unit

_PUBLIC = ('Stage', 'StageAborted', 'StageFailed', 'run_stages',
           'run_independent_stages',
           'load_state', 'stage_is_done', 'stage_artifact', 'STATE_VERSION')


def test_new_home_exports_the_full_contract():
    from lib.production import stages
    for name in _PUBLIC:
        assert hasattr(stages, name), name


def test_package_exports_the_owner_module_objects():
    import lib.production as facade
    from lib.production import stages as home
    for name in _PUBLIC:
        assert getattr(home, name) is getattr(facade, name)


def test_background_llm_policy_is_finite_abortable_and_shared(monkeypatch):
    import lib.production as facade
    from lib.production import llm_policy

    monkeypatch.setenv('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', '7')
    event = threading.Event()
    abort_check = llm_policy.abort_check_from_event(event)
    kwargs = llm_policy.production_llm_dispatch_kwargs(
        abort_check=abort_check)

    assert kwargs['max_retries'] == 2
    assert kwargs['max_429_attempts'] == 7
    assert kwargs['abort_check']() is False
    event.set()
    assert kwargs['abort_check']() is True
    assert llm_policy.production_llm_max_429_attempts(10_000) == 64
    assert llm_policy.optional_llm_dispatch_kwargs({
        'TOFU_DEPLOYMENT_MODE': 'personal',
    }) == {
        'max_429_attempts': 2,
        'defer_on_shared_contention': True,
    }
    assert (facade.optional_llm_dispatch_kwargs
            is llm_policy.optional_llm_dispatch_kwargs)
    assert (facade.optional_llm_max_429_attempts
            is llm_policy.optional_llm_max_429_attempts)
    assert (facade.production_llm_dispatch_kwargs
            is llm_policy.production_llm_dispatch_kwargs)


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '4'])
def test_background_llm_policy_rejects_invalid_explicit_budget(value):
    from lib.production.llm_policy import production_llm_max_429_attempts

    with pytest.raises(ValueError, match='positive integer'):
        production_llm_max_429_attempts(value)


def test_background_image_policy_is_finite_probed_and_shared(monkeypatch):
    import lib.production as facade
    from lib.production import image_policy

    monkeypatch.setenv('TOFU_PRODUCTION_IMAGE_FANOUT', '3')
    monkeypatch.setenv('TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS', '7')
    abort_check = lambda: False
    kwargs = image_policy.production_image_dispatch_kwargs(
        abort_check=abort_check)

    assert image_policy.production_image_fanout() == 3
    assert kwargs == {'max_retries': 1, 'max_429_attempts': 7,
                      'abort_check': abort_check}
    assert image_policy.production_image_fanout(10_000) == 4
    assert image_policy.production_image_max_429_attempts(10_000) == 64
    assert (facade.production_image_dispatch_kwargs
            is image_policy.production_image_dispatch_kwargs)


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '4'])
def test_background_image_policy_rejects_invalid_explicit_budget(value):
    from lib.production.image_policy import (
        production_image_fanout,
        production_image_max_429_attempts,
    )

    with pytest.raises(ValueError, match='positive integer'):
        production_image_fanout(value)
    with pytest.raises(ValueError, match='positive integer'):
        production_image_max_429_attempts(value)


def test_substrate_is_capability_agnostic():
    """lib/production/stages.py must not import video/audio/LLM modules.

    The whole point of the substrate is that the NEXT recipe (podcast, PPT,
    long report) can ride it without inheriting motion-video baggage.
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'lib', 'production', 'stages.py')
    tree = ast.parse(open(path, encoding='utf-8').read())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    banned = ('motion_video', 'tts', 'llm', 'paper', 'ffmpeg', 'audio')
    for mod in imported:
        for token in banned:
            assert token not in mod, f'substrate imports {mod!r} (banned: {token})'


def test_behaviour_unchanged_through_new_path(tmp_path):
    """The relocated runner still checkpoints + resumes identically."""
    from lib.production import Stage, run_stages, load_state, stage_is_done

    state_path = str(tmp_path / 'state.json')
    calls = []

    def boom(ctx):
        calls.append('b1')
        raise RuntimeError('crash')

    from lib.production import StageFailed
    with pytest.raises(StageFailed):
        run_stages([Stage('a', lambda c: calls.append('a1') or {'v': 1}),
                    Stage('b', boom)], {}, state_path=state_path)
    assert calls == ['a1', 'b1']
    assert stage_is_done(load_state(state_path), 'a')

    calls.clear()
    run_stages([Stage('a', lambda c: calls.append('a2') or {'v': 9}),
                Stage('b', lambda c: calls.append('b2') or {'v': 2})],
               {}, state_path=state_path)
    assert calls == ['b2']  # 'a' resumed from the checkpoint, not re-run


def test_expired_or_versioned_checkpoint_reruns_the_dependency_suffix(
        tmp_path, monkeypatch):
    """Fresh research must never be combined with an old downstream deck."""
    from lib.production import stages as st

    now = {'value': 1000.0}
    monkeypatch.setattr(st.time, 'time', lambda: now['value'])
    state_path = str(tmp_path / 'state.json')
    calls = []

    def graph(label, revision='facts-v1'):
        return [
            st.Stage('research',
                     lambda c: calls.append(f'research-{label}') or {'v': label},
                     resume_ttl_s=10, checkpoint_version=revision),
            st.Stage('outline',
                     lambda c: calls.append(f'outline-{label}') or {
                         'from': c['artifacts']['research']['v']}),
        ]

    st.run_stages(graph('first'), {}, state_path=state_path)
    assert calls == ['research-first', 'outline-first']

    calls.clear()
    now['value'] = 1005.0
    out = st.run_stages(graph('fresh'), {}, state_path=state_path)
    assert calls == []
    assert out['outline']['from'] == 'first'

    calls.clear()
    now['value'] = 1020.0
    out = st.run_stages(graph('expired'), {}, state_path=state_path)
    assert calls == ['research-expired', 'outline-expired']
    assert out['outline']['from'] == 'expired'

    calls.clear()
    now['value'] = 1021.0
    out = st.run_stages(graph('revision', 'facts-v2'), {},
                        state_path=state_path)
    assert calls == ['research-revision', 'outline-revision']
    assert out['outline']['from'] == 'revision'


def test_independent_stages_start_beyond_the_idle_hint_and_checkpoint_each_result(tmp_path):
    from lib.production import stages as st

    state_path = str(tmp_path / 'state.json')
    lock = threading.Lock()
    first_pair = threading.Barrier(3)
    state = {'active': 0, 'peak': 0, 'calls': []}

    def run(name):
        def _run(_ctx):
            with lock:
                state['active'] += 1
                state['peak'] = max(state['peak'], state['active'])
                state['calls'].append(name)
            try:
                first_pair.wait(timeout=2)
                return {'name': name}
            finally:
                with lock:
                    state['active'] -= 1
        return _run

    stages = [st.Stage(name, run(name)) for name in ('a', 'b', 'c')]
    artifacts = st.run_independent_stages(
        stages, {}, state_path=state_path, max_workers=1)

    assert state['peak'] == 3
    assert set(state['calls']) == {'a', 'b', 'c'}
    assert {name: artifacts[name] for name in ('a', 'b', 'c')} == {
        name: {'name': name} for name in ('a', 'b', 'c')}
    checkpoint = st.load_state(state_path)
    assert all(st.stage_is_done(checkpoint, name) for name in ('a', 'b', 'c'))

    skipped = []
    st.run_independent_stages(
        [st.Stage(name, lambda _ctx, n=name: skipped.append(n))
         for name in ('a', 'b', 'c')],
        {}, state_path=state_path, max_workers=1)
    assert skipped == []


def test_independent_failure_preserves_all_already_running_successes(
        tmp_path):
    from lib.production import stages as st

    state_path = str(tmp_path / 'state.json')
    failure_observed = threading.Event()
    calls = []
    entered = threading.Barrier(3)

    def fail(_ctx):
        calls.append('a')
        entered.wait(timeout=2)
        raise RuntimeError('broken section')

    def finish_inflight(_ctx):
        calls.append('b')
        entered.wait(timeout=2)
        assert failure_observed.wait(timeout=2)
        return {'ok': 'b'}

    def finish_other(_ctx):
        calls.append('c')
        entered.wait(timeout=2)
        assert failure_observed.wait(timeout=2)
        return {'ok': 'c'}

    def emit(event):
        if event.get('type') == 'stage_failed' and event.get('stage') == 'a':
            failure_observed.set()

    with pytest.raises(st.StageFailed) as exc_info:
        st.run_independent_stages(
            [st.Stage('a', fail), st.Stage('b', finish_inflight),
             st.Stage('c', finish_other)],
            {}, state_path=state_path, max_workers=2, emit=emit)
    assert exc_info.value.stage == 'a'
    assert set(calls) == {'a', 'b', 'c'}
    checkpoint = st.load_state(state_path)
    assert st.stage_is_done(checkpoint, 'b')
    assert not st.stage_is_done(checkpoint, 'a')
    assert st.stage_is_done(checkpoint, 'c')

    calls.clear()
    st.run_independent_stages(
        [st.Stage('a', lambda _ctx: calls.append('a') or {'ok': 'a'}),
         st.Stage('b', lambda _ctx: calls.append('b') or {'ok': 'b'}),
         st.Stage('c', lambda _ctx: calls.append('c') or {'ok': 'c'})],
        {}, state_path=state_path, max_workers=2)
    assert calls == ['a']


def test_checkpoint_write_failure_rolls_back_artifact_and_reports_stage_failure(
        tmp_path, monkeypatch):
    from lib import json_store
    from lib.production import stages as st

    ctx = {}
    events = []

    def fail_write(_path, _state):
        raise OSError('disk unavailable')

    monkeypatch.setattr(json_store, 'write_json_atomic', fail_write)
    with pytest.raises(st.StageFailed, match='checkpoint commit failed'):
        st.run_stages(
            [st.Stage('section', lambda _ctx: {'body': 'computed'})], ctx,
            state_path=str(tmp_path / 'state.json'), emit=events.append)

    assert ctx['artifacts'] == {}
    assert [event['type'] for event in events][-1] == 'stage_failed'


def test_abort_exception_does_not_spend_a_stage_retry(tmp_path):
    from lib.production import stages as st

    signal = {'aborted': False}
    calls = []

    def interrupted(_ctx):
        calls.append('attempt')
        signal['aborted'] = True
        raise RuntimeError('transport interrupted by abort')

    with pytest.raises(st.StageAborted):
        st.run_stages(
            [st.Stage('model-call', interrupted, retry=3)], {},
            state_path=str(tmp_path / 'state.json'),
            abort_check=lambda: signal['aborted'])

    assert calls == ['attempt']


def test_independent_version_change_preserves_siblings_and_invalidates_dependent(
        tmp_path):
    from lib.production import stages as st

    state_path = str(tmp_path / 'state.json')
    st.run_independent_stages(
        [st.Stage('a', lambda _ctx: {'v': 'a'}, checkpoint_version='a-v1'),
         st.Stage('b', lambda _ctx: {'v': 'b1'}, checkpoint_version='b-v1')],
        {}, state_path=state_path, max_workers=2,
        dependent_stage_names=('assemble',))
    st.run_stages(
        [st.Stage('assemble', lambda _ctx: {'v': 'old'})], {},
        state_path=state_path)

    calls = []
    artifacts = st.run_independent_stages(
        [st.Stage('a', lambda _ctx: calls.append('a') or {'v': 'new-a'},
                  checkpoint_version='a-v1'),
         st.Stage('b', lambda _ctx: calls.append('b') or {'v': 'b2'},
                  checkpoint_version='b-v2')],
        {}, state_path=state_path, max_workers=2,
        dependent_stage_names=('assemble',))

    assert calls == ['b']
    assert artifacts['a'] == {'v': 'a'}
    checkpoint = st.load_state(state_path)
    assert not st.stage_is_done(checkpoint, 'assemble')
    assert st.stage_artifact(checkpoint, 'b') == {'v': 'b2'}


def test_recipe_imports_the_shared_stage_owner():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        'lib', 'motion_video', '_recipe.py')
    src = open(path, encoding='utf-8').read()
    assert 'from lib.production.stages import' in src
    assert 'from lib.motion_video._stages import' not in src


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'CLAUDE.md')),
    reason='CLAUDE.md not shipped in opensource (agent-rules doc, export-excluded)')
def test_claude_md_documents_the_new_packages():
    """The short agent entry point must lead to the authoritative domain map."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    claude = open(os.path.join(root, 'CLAUDE.md'), encoding='utf-8').read()
    docs_map = open(os.path.join(root, 'docs', 'README.md'),
                    encoding='utf-8').read()
    production = open(os.path.join(root, 'docs', 'modules', 'production.md'),
                      encoding='utf-8').read()
    assert '`docs/README.md`' in claude
    assert '`lib/production/`' in docs_map
    assert 'modules/production.md' in docs_map
    assert '`lib/production/stages.py`' in production
    assert '`lib/motion_video/`' in production


def test_sixty_four_stages_enter_one_idle_hint_and_reap_workers(tmp_path, monkeypatch):
    import time
    from lib.production import stages as st
    barrier = threading.Barrier(64)
    caller = threading.get_ident()
    original_commit = st._commit
    commits = []
    def commit(*args):
        assert threading.get_ident() == caller
        commits.append(args[2].name)
        return original_commit(*args)
    monkeypatch.setattr(st, '_commit', commit)
    def run(index):
        def execute(ctx):
            assert ctx['artifacts']['upstream'] == {'v':1}
            barrier.wait(timeout=5)
            return {'index':index}
        return execute
    began = time.monotonic()
    state_path = str(tmp_path / 'state.json')
    artifacts = st.run_independent_stages(
        [st.Stage(f'section-{i}',run(i)) for i in range(64)],
        {'artifacts':{'upstream':{'v':1}}}, state_path=state_path, max_workers=1)
    elapsed = time.monotonic()-began
    assert len(commits) == len(set(commits)) == 64
    assert all(artifacts[f'section-{i}'] == {'index':i} for i in range(64))
    assert all(st.stage_is_done(st.load_state(state_path),f'section-{i}') for i in range(64))
    assert not [t for t in threading.enumerate() if t.name.startswith('production-stage_')]
    print(f'production-demand-stage callers=64 idle_hint=1 checkpoints=64 settled_ms={elapsed*1000:.2f} retained_threads=0')


def test_unstarted_worker_failure_preserves_running_checkpoint_and_stops_submission(tmp_path, monkeypatch):
    import threading
    from lib.production import stages as st, concurrency
    entered = threading.Event()
    release = threading.Event()
    original_factory = concurrency.production_executor
    original_start = threading.Thread.start
    from contextlib import contextmanager
    @contextmanager
    def observed_factory(**kwargs):
        with original_factory(**kwargs) as executor:
            submit = executor.submit
            def observed_submit(fn, stage, *args, **kwargs):
                future = submit(fn, stage, *args, **kwargs)
                if stage.name == 'a':
                    assert entered.wait(timeout=2)
                return future
            executor.submit = observed_submit
            yield executor
    monkeypatch.setattr(concurrency, 'production_executor', observed_factory)
    def start(thread):
        if thread.name == 'production-stage_1':
            raise RuntimeError('injected physical thread failure')
        return original_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    calls = []
    def running(ctx):
        calls.append('a')
        entered.set()
        assert release.wait(timeout=2)
        return {'ok':True}
    def emit(event):
        if event['type']=='stage_failed':release.set()
    path = str(tmp_path/'state.json')
    with pytest.raises(st.StageFailed, match='worker could not start') as error:
        st.run_independent_stages([st.Stage('a',running),
            st.Stage('b',lambda ctx:calls.append('b')),
            st.Stage('c',lambda ctx:calls.append('c'))], {}, state_path=path,
            max_workers=1, emit=emit)
    assert error.value.stage == 'b' and calls == ['a']
    assert set(st.load_state(path)['stages']) == {'a'}
    assert not [t for t in threading.enumerate() if t.name.startswith('production-stage_')]


def test_declared_future_cancel_error_retains_identity_and_does_not_retry(tmp_path):
    from concurrent.futures import CancelledError
    from lib.production import stages as st
    original = CancelledError('owner cancelled')
    calls = []
    def abort(ctx):
        calls.append(1)
        raise original
    with pytest.raises(CancelledError) as error:
        st.run_independent_stages([st.Stage('abort',abort,retry=3)], {},
            state_path=str(tmp_path/'state.json'), max_workers=1,
            abort_exceptions=(CancelledError,))
    assert error.value is original and calls == [1]


def test_failed_parallel_checkpoint_is_not_resurrected_by_another_commit(tmp_path, monkeypatch):
    from lib import json_store
    from lib.production import stages as st
    barrier = threading.Barrier(2)
    original_write = json_store.write_json_atomic
    def write(path,state):
        if 'a' in state.get('stages',{}):raise OSError('injected checkpoint failure')
        return original_write(path,state)
    monkeypatch.setattr(json_store,'write_json_atomic',write)
    def run(ctx):
        barrier.wait(timeout=2)
        return {'ok':True}
    ctx = {};path=str(tmp_path/'state.json')
    with pytest.raises(st.StageFailed, match='checkpoint commit failed'):
        st.run_independent_stages([st.Stage('a',run),st.Stage('b',run)],ctx,
            state_path=path,max_workers=1)
    assert set(st.load_state(path)['stages']) == {'b'}
    assert set(ctx['artifacts']) == {'b'}


def test_concurrent_production_pools_aggregate_owned_observations(monkeypatch):
    from lib.production import concurrency
    snapshots = []
    monkeypatch.setattr(concurrency,'publish_executor_state',lambda name,**state:snapshots.append(state))
    first_entered=threading.Barrier(3);second_entered=threading.Event()
    first_release=threading.Event();second_release=threading.Event()
    def first():
        first_entered.wait(timeout=2)
        assert first_release.wait(timeout=2)
    def second():
        second_entered.set()
        assert second_release.wait(timeout=2)
    with concurrency.production_executor(name='production-stage',idle_reuse_hint=1) as peer:
        with concurrency.production_executor(name='production-stage',idle_reuse_hint=1) as initial:
            calls=[initial.submit(first) for _ in range(2)]
            first_entered.wait(timeout=2)
            remaining=peer.submit(second)
            assert second_entered.wait(timeout=2)
            assert snapshots[-1]['active']==3
            first_release.set()
            for call in calls:call.result(timeout=2)
        assert snapshots[-1]['active']==1
        assert snapshots[-1]['resident_threads']==1
        second_release.set();remaining.result(timeout=2)
    assert snapshots[-1]['active']==snapshots[-1]['resident_threads']==0
    assert not any(owner.name == 'production-stage' for owner in concurrency._POOL_SNAPSHOTS)


def test_observation_outage_does_not_block_owned_stage_work(tmp_path, monkeypatch):
    from lib.production import concurrency, stages as st
    def fail(*args, **kwargs):raise RuntimeError('metrics unavailable')
    monkeypatch.setattr(concurrency,'publish_executor_state',fail)
    artifacts = st.run_independent_stages([st.Stage('a',lambda ctx:{'ok':True})],{},
        state_path=str(tmp_path/'state.json'),max_workers=1)
    assert artifacts['a']=={'ok':True}
    assert not any(owner.name == 'production-stage' for owner in concurrency._POOL_SNAPSHOTS)


def test_owned_results_start_sixty_four_jobs_with_one_idle_hint():
    from lib.production.concurrency import owned_results, _POOL_SNAPSHOTS
    import time
    gate=threading.Barrier(64)
    caller=threading.get_ident()
    def run(index):
        gate.wait(timeout=5)
        return index
    began=time.monotonic()
    completed=[]
    with owned_results(range(64),run,name='media-burst',idle_reuse_hint=1) as outcomes:
        for index,future in outcomes:
            assert threading.get_ident()==caller
            completed.append(future.result())
    assert sorted(completed)==list(range(64))
    assert not [t for t in threading.enumerate() if t.name.startswith('media-burst_')]
    assert not any(owner.name in {'media-burst','media-error'} for owner in _POOL_SNAPSHOTS)
    print(f'media-demand jobs=64 idle_hint=1 settled_ms={(time.monotonic()-began)*1000:.2f} retained_threads=0')


def test_owned_results_drains_started_work_on_consumer_error():
    from lib.production.concurrency import owned_results, _POOL_SNAPSHOTS
    entered=threading.Barrier(2)
    released=threading.Event()
    finished=threading.Event()
    def run(index):
        entered.wait(timeout=2)
        if index==1:
            assert released.wait(timeout=2)
            finished.set()
        return index
    with pytest.raises(RuntimeError,match='consumer failed'):
        with owned_results(range(2),run,name='media-error',idle_reuse_hint=1) as outcomes:
            _,future=next(iter(outcomes))
            assert future.result()==0
            assert not finished.is_set()
            released.set()
            raise RuntimeError('consumer failed')
    assert finished.is_set()
    assert not [t for t in threading.enumerate() if t.name.startswith('media-error_')]
    assert not any(owner.name in {'media-burst','media-error'} for owner in _POOL_SNAPSHOTS)


def test_owned_results_preserves_and_isolates_caller_context():
    from contextvars import ContextVar
    from lib.production.concurrency import owned_results
    marker=ContextVar('demand_context_marker')
    authority=object();token=marker.set(authority)
    gate=threading.Barrier(16)
    def run(index):
        assert marker.get() is authority
        marker.set(index)
        gate.wait(timeout=3)
        return marker.get()
    try:
        with owned_results(range(16),run,name='context-probe',idle_reuse_hint=1,
                           inherit_context=True) as outcomes:
            result={index:future.result() for index,future in outcomes}
        assert result=={index:index for index in range(16)}
        assert marker.get() is authority
    finally:marker.reset(token)
