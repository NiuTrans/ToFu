"""Automatic native fastpath: selection, outcome records, capacity and planned exit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from lib.storage_authority_sql import native_fastpath_auto as auto
from serverctl_pkg import local_placement

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name != 'posix', reason='native fastpath is POSIX-only'),
]

GIB = 1024 ** 3


@pytest.fixture
def layout(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    authority = data_dir / 'tofudb-authority'
    authority.mkdir(parents=True)
    base = tmp_path / 'local-base'
    base.mkdir()
    remote = local_placement.Volume(filesystem_type='fuse.bgfuse', storage_class='network-filesystem')
    monkeypatch.setattr(auto.local_placement, 'describe_volume', lambda path, **_: remote)
    return SimpleNamespace(data_dir=data_dir, authority=authority, base=base)


def _seed_state(layout, **records):
    """Write records into the persisted decision document (its public format)."""
    path = auto.state_path(layout.data_dir)
    state = json.loads(path.read_text()) if path.exists() else {'format': auto.STATE_FORMAT}
    state.update(records)
    path.write_text(json.dumps(state))


def _census(layout, *, blocks=10_000, corpus_bytes=GIB, publish_us=900.0, at=None):
    census = {'authority': str(layout.authority), 'measuredAt': time.time() if at is None else at,
              'estimatedBlocks': blocks, 'estimatedBytes': corpus_bytes,
              'sourcePublishMicros': publish_us}
    _seed_state(layout, census=census)
    return census


def _placement(root=None, *, selected=True, reason='selected', source_us=330.0, target_us=4.0):
    def choose(source_path, purpose, *, environ, required_bytes):
        choose.required_bytes = required_bytes
        assert purpose == local_placement.PURPOSE_NATIVE_AUTHORITY
        return local_placement.Placement(selected, purpose, 'auto', reason,
                                         root=str(root) if selected else '',
                                         source_latency_us=source_us, target_latency_us=target_us)
    return choose


def _statvfs(files=16_777_216, available=16_000_000):
    return lambda _path: SimpleNamespace(f_files=files, f_favail=available)


def _probes(durable_fsync=1940.0, local_fsync=2390.0, **overrides):
    probes = {
        'fsync_probe': lambda path: durable_fsync if 'tofudb-authority' in str(path) else local_fsync,
        'publish_probe': lambda path: 4300.0,
        'lookup_probe': lambda path: None,
        'statvfs': _statvfs(),
    }
    probes.update(overrides)
    return probes


def _plan(layout, environ=None, **overrides):
    options = {'transport': 'daemon', 'placement': _placement(layout.base), **_probes()}
    options.update(overrides)
    return auto.plan_launch(layout.data_dir, environ or {}, **options)


def _fence(layout, name='FASTPATH'):
    namespace = 'ab' * 32
    marker = {'format': 'tofu.native-fastpath.v1',
              'local_authority': str(layout.base / 'tofudb-fastpath' / namespace),
              'shadow_directory': str(layout.data_dir / 'tofudb-fastpath-shadow' / namespace)}
    (layout.authority / name).write_text(json.dumps(marker))
    return marker


def test_probe_rule_mirrors_the_engine_contract_vectors():
    # Same vectors as fastpath::tests::candidate_probe_admits_a_commit_or_bounded_metadata_win_only.
    assert auto.engine_admits(1_900, 120, 330, 300)
    assert auto.engine_admits(1_940, 2_390, 332, 4)
    assert not auto.engine_admits(1_000, 2_500, 330, 4)
    assert not auto.engine_admits(1_900, 1_000, 300, 150)


def test_measured_metadata_bound_authority_is_nominated_for_auto_admission(layout):
    _census(layout)
    choose = _placement(layout.base)

    plan = _plan(layout, placement=choose)

    assert (plan.selected, plan.reason, plan.base) == (True, 'selected', str(layout.base))
    assert choose.required_bytes == 2 * GIB, 'corpus plus equal growth room'
    assert plan.environment() == {'TOFU_DB_FASTPATH_DIR': str(layout.base),
                                  'TOFU_DB_FASTPATH_ADMISSION': 'auto'}
    assert plan.startup_timeout == auto.seed_startup_timeout({})
    assert [check['check'] for check in plan.checks][-3:] == ['inodes', 'activation-cost', 'speedup']


@pytest.mark.parametrize('environ, overrides, reason', [
    ({'TOFU_DB_FASTPATH': 'off'}, {}, 'disabled'),
    ({'TOFU_DB_DURABILITY': 'normal'}, {}, 'durability-override'),
    ({'TOFU_DB_DISABLE_ONLINE_BACKGROUND': '1'}, {}, 'online-background-disabled'),
    ({}, {'transport': 'embedded'}, 'embedded-transport'),
    ({}, {'placement': _placement(selected=False, reason='insufficient-space')},
     'placement-insufficient-space'),
    ({}, {'placement': _placement(selected=False, reason='candidate-persistence-unknown')},
     'placement-candidate-persistence-unknown'),
    ({}, {'statvfs': _statvfs(files=1_000_000, available=60_000)}, 'inode-capacity'),
    ({}, {'fsync_probe': lambda path: 1_000.0 if 'tofudb-authority' in str(path) else 2_500.0},
     'speedup-insufficient'),
])
def test_unsuitable_launches_stay_classic_with_an_explained_reason(layout, environ, overrides, reason):
    _census(layout)

    plan = _plan(layout, environ, **overrides)

    assert (plan.selected, plan.reason) == (False, reason)
    assert plan.environment() == {}
    assert plan.detail


def test_this_hosts_corpus_is_refused_for_its_seed_cost(layout):
    """2026-10-27 figures: ~68M blocks, 5.5 ms FUSE / 4.3 ms overlay publications."""
    _census(layout, blocks=68_000_000, corpus_bytes=1_200 * GIB, publish_us=5_548.0)

    plan = _plan(layout, placement=_placement(layout.base),
                 statvfs=_statvfs(files=10 ** 9, available=9 * 10 ** 8))

    assert (plan.selected, plan.reason) == (False, 'activation-too-costly')
    assert 'h)' in plan.detail


def test_unmeasured_corpus_is_never_relocated(layout):
    assert _plan(layout).reason == 'census-pending'
    _census(layout, at=time.time() - auto.CENSUS_MAX_AGE_SECONDS - 1)
    assert _plan(layout).reason == 'census-pending'


def test_operator_consent_skips_python_speed_probes_but_not_capacity(layout):
    _census(layout)

    def no_probe(path):
        raise AssertionError('consent mode leaves the speed decision to the engine probe')

    plan = _plan(layout, {'TOFU_DB_FASTPATH': 'on'}, fsync_probe=no_probe)
    assert (plan.selected, plan.reason) == (True, 'selected')
    refused = _plan(layout, {'TOFU_DB_FASTPATH': 'on'}, fsync_probe=no_probe,
                    statvfs=_statvfs(available=100))
    assert refused.reason == 'inode-capacity'


def test_explicit_selection_is_left_to_required_engine_admission(layout):
    plan = _plan(layout, {'TOFU_DB_FASTPATH_DIR': str(layout.base)})
    assert (plan.selected, plan.reason, plan.environment()) == (False, 'explicit', {})


@pytest.mark.parametrize('mode', ['auto', 'on', 'off'])
def test_a_fenced_authority_always_resumes_its_recorded_front(layout, mode):
    _fence(layout)

    plan = _plan(layout, {'TOFU_DB_FASTPATH': mode})

    assert (plan.selected, plan.reason, plan.base) == (True, 'fenced-resume', str(layout.base))
    assert plan.environment()['TOFU_DB_FASTPATH_DIR'] == str(layout.base)
    assert ('planned exit' in plan.detail) == (mode == 'off')


def test_an_unreadable_fence_is_never_reinterpreted(layout):
    (layout.authority / 'FASTPATH').write_text('{damaged')
    plan = _plan(layout)
    assert (plan.selected, plan.reason) == (False, 'fenced-unreadable')


def test_interrupted_preparation_resumes_once_then_waits_for_review(layout):
    _fence(layout, 'FASTPATH_PENDING')
    plan = _plan(layout)
    assert (plan.selected, plan.reason) == (True, 'preparation-resume')
    auto.record_launch(layout.data_dir, plan, None, {'reason': 'stale-preparation', 'detail': 'x'})
    assert _plan(layout).reason == 'preparation-blocked'


def test_projection_inside_the_front_base_is_refused(layout):
    _census(layout)
    plan = _plan(layout, projection_directory=str(layout.base / 'projection'))
    assert plan.reason == 'projection-overlap'


def test_launch_records_engine_outcome_and_keeps_the_front_origin(layout):
    _census(layout)
    plan = _plan(layout)
    auto.record_launch(layout.data_dir, plan, None, {'reason': 'speedup-insufficient', 'detail': 'p'})
    last = auto.load_state(layout.data_dir)['lastLaunch']
    assert (last['outcome'], last['engineReason']) == ('engine-declined', 'speedup-insufficient')

    marker = _fence(layout)
    witness = {'localAuthority': marker['local_authority'],
               'shadowDirectory': marker['shadow_directory']}
    auto.record_launch(layout.data_dir, plan, witness)
    assert auto.load_state(layout.data_dir)['activeFront']['origin'] == 'automatic'
    resume = _plan(layout)
    auto.record_launch(layout.data_dir, resume, witness)
    state = auto.load_state(layout.data_dir)
    assert (state['lastLaunch']['outcome'], state['activeFront']['origin']) == ('active', 'automatic')


def test_explicit_activation_is_recorded_as_explicit(layout):
    marker = _fence(layout)
    plan = _plan(layout, {'TOFU_DB_FASTPATH_DIR': str(layout.base)})
    auto.record_launch(layout.data_dir, plan, {'localAuthority': marker['local_authority']})
    state = auto.load_state(layout.data_dir)
    assert (state['lastLaunch']['outcome'], state['activeFront']['origin']) == ('active', 'explicit')


def test_outer_budget_extends_only_when_this_boot_could_seed(layout):
    assert not auto.may_seed(layout.data_dir, {})
    _census(layout)
    assert auto.may_seed(layout.data_dir, {})
    declined = _plan(layout, placement=_placement(selected=False, reason='insufficient-space'))
    auto.record_launch(layout.data_dir, declined, None)
    assert not auto.may_seed(layout.data_dir, {}), 'same corpus and settings already declined'
    assert auto.may_seed(layout.data_dir, {'TOFU_LOCAL_DIR': str(layout.base)})
    assert not auto.may_seed(layout.data_dir, {'TOFU_DB_FASTPATH': 'off'})
    assert auto.may_seed(layout.data_dir, {'TOFU_DB_FASTPATH_DIR': str(layout.base)})
    _fence(layout)
    assert auto.may_seed(layout.data_dir, {'TOFU_DB_FASTPATH': 'off'})


def test_lifespan_budget_covers_a_planned_exit_and_a_seed(monkeypatch):
    from lib.storage import startup_budget

    # The full-backup deadline floors at 1800 s, so a long seed budget is the
    # case where the automatic extension decides the outer lifespan.
    environment = {'TOFU_STORAGE_SQLITE_BACKUP_TIMEOUT_SECONDS': '1800',
                   'TOFU_STORAGE_FASTPATH_STARTUP_TIMEOUT_S': '3600'}
    monkeypatch.setattr(auto, 'may_seed', lambda data_dir, environ: False)
    assert startup_budget.lifespan_startup_timeout(environment) == 1860.0
    monkeypatch.setattr(auto, 'may_seed', lambda data_dir, environ: True)
    assert startup_budget.lifespan_startup_timeout(environment) == 30.0 + 3600.0 + 60.0
    explicit = {**environment, 'TOFU_DB_FASTPATH_DIR': '/explicit/base'}
    assert startup_budget.lifespan_startup_timeout(explicit) == 3600.0 + 60.0, (
        'an explicit selection keeps its single seed budget')


def test_census_estimates_the_corpus_from_bounded_samples(tmp_path):
    authority = tmp_path / 'tofudb-authority'
    for shard in ('00', '40', '80', 'c0'):
        directory = authority / 'blocks' / shard
        directory.mkdir(parents=True)
        for index in range(10):
            (directory / f'{shard}{index:062x}').write_bytes(b'x' * 1024)
    segments = authority / 'payload-segments'
    segments.mkdir()
    for index in range(3):
        (segments / f'{index:032x}.pseg').write_bytes(b'y' * 65536)
    (authority / 'CONTROL').write_bytes(b'c' * 8192)

    census = auto.take_census(authority, now=5.0)

    assert (census['looseBlockFiles'], census['payloadSegmentFiles']) == (40, 3)
    assert census['estimatedBlocks'] == 40 + 3 * 64, 'packed bytes are republished as blocks'
    assert census['estimatedBytes'] == 40 * 1024 + 3 * 65536 + 8192


_TOTAL_GIB = 500
# Free space is expressed above the shared placement reserve, so a change to
# that policy moves the fixtures with it instead of pinning a magic number.
_RESERVE_GIB = local_placement.reserve_bytes(_TOTAL_GIB * GIB) / GIB


def _usage(headroom_gib):
    return lambda _path: SimpleNamespace(free=(_RESERVE_GIB + headroom_gib) * GIB,
                                         total=_TOTAL_GIB * GIB)


def test_capacity_pressure_relieves_cotenants_then_requests_the_planned_exit(layout):
    front = layout.base / 'tofudb-fastpath' / 'ns'
    front.mkdir(parents=True)
    _census(layout, blocks=1_000, corpus_bytes=40 * GIB)
    witness = {'localAuthority': str(front)}
    relieved = []

    def govern(free_gib, environ=None):
        return auto.govern(layout.data_dir, witness, environ or {}, disk_usage=_usage(free_gib),
                           statvfs=_statvfs(), relieve=lambda path: relieved.append(path) or {})

    assert govern(100)['state'] == 'ok' and not relieved
    assert govern(15)['state'] == 'pressure' and relieved == [front]
    assert 'exitRequested' not in auto.load_state(layout.data_dir)
    assert govern(4, {'TOFU_DB_FASTPATH_DIR': str(layout.base)})['state'] == 'critical'
    assert 'exitRequested' not in auto.load_state(layout.data_dir), 'explicit fronts are never exited'
    assert govern(4)['state'] == 'critical'
    assert auto.load_state(layout.data_dir)['exitRequested']['reason'] == 'capacity'


def test_inode_exhaustion_is_capacity_critical(layout):
    front = layout.base / 'tofudb-fastpath' / 'ns'
    front.mkdir(parents=True)
    _census(layout, blocks=100_000, corpus_bytes=GIB)
    status = auto.govern(layout.data_dir, {'localAuthority': str(front)}, {},
                         disk_usage=_usage(400), statvfs=_statvfs(files=1_000_000, available=60_000),
                         relieve=lambda path: {})
    assert status['state'] == 'critical' and status['inodeHeadroom'] == 10_000


def _fake_engine(calls, *, export_ok=True):
    def run(argv, **kwargs):
        calls.append(argv[1])
        assert kwargs['timeout'] > 0
        if argv[1] == 'fastpath-export':
            if not export_ok:
                return SimpleNamespace(returncode=1, stdout='', stderr='export refused')
            target = Path(argv[argv.index('--backup-dir') + 1])
            target.mkdir()
            (target / 'CONTROL').write_bytes(b'exported')
            return SimpleNamespace(returncode=0, stderr='', stdout=(
                'authority_uuid=u durable_sequence=9 export_verified=true source_retained=true\n'))
        assert argv[1] == 'inspect'
        return SimpleNamespace(returncode=0, stderr='', stdout='format=tofu-db.v1 authority_uuid=u\n')
    return run


def _active_front(layout, origin='automatic', request=True):
    marker = _fence(layout)
    front, shadow = Path(marker['local_authority']), Path(marker['shadow_directory'])
    front.mkdir(parents=True)
    (front / 'CONTROL').write_bytes(b'front')
    shadow.mkdir(parents=True)
    (layout.authority / 'CONTROL').write_bytes(b'seed')
    requested = {'exitRequested': {'reason': 'capacity', 'at': 1.0}} if request else {}
    _seed_state(layout, activeFront={'localAuthority': str(front), 'origin': origin}, **requested)
    return front, shadow


def test_capacity_exit_exports_installs_and_releases_the_small_tier(layout):
    front, shadow = _active_front(layout)
    calls = []

    journal = auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db', {}, timeout=60,
                                         run=_fake_engine(calls))

    assert calls == ['fastpath-export', 'inspect']
    assert (journal['phase'], journal['reason']) == ('complete', 'capacity')
    assert (layout.authority / 'CONTROL').read_bytes() == b'exported'
    assert not (layout.authority / 'FASTPATH').exists()
    retired = Path(journal['retiredSeed'])
    assert (retired / 'FASTPATH').exists() and (retired / 'CONTROL').read_bytes() == b'seed'
    assert not front.exists(), 'the exported front no longer occupies the small tier'
    assert not shadow.exists() and Path(f"{shadow}.retired-{journal['stamp']}").is_dir()
    state = auto.load_state(layout.data_dir)
    assert not {'exitRequested', 'activeFront', 'census'} & set(state)
    _census(layout)
    assert _plan(layout).reason == 'capacity-exit-cooldown'


def test_failed_export_keeps_serving_the_front_and_backs_off(layout):
    front, _ = _active_front(layout)
    calls = []

    failed = auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db', {}, timeout=60,
                                        run=_fake_engine(calls, export_ok=False))

    assert failed['phase'] == 'failed' and 'export refused' in failed['error']
    assert (layout.authority / 'FASTPATH').exists() and (front / 'CONTROL').exists()
    assert auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db', {}, timeout=60,
                                      run=_fake_engine(calls)) is None
    assert calls == ['fastpath-export'], 'no retry inside the backoff window'
    assert _plan(layout).reason == 'fenced-resume'


def test_interrupted_swap_resumes_without_exporting_again(layout):
    front, shadow = _active_front(layout)
    target = layout.data_dir / 'tofudb-authority.fastpath-exit-X'
    target.mkdir()
    (target / 'CONTROL').write_bytes(b'exported')
    retired = layout.data_dir / 'tofudb-authority.fastpath-seed-X'
    layout.authority.rename(retired)
    journal = {'phase': 'swapping', 'reason': 'capacity', 'stamp': 'X', 'startedAt': 1.0,
               'target': str(target), 'retiredSeed': str(retired),
               'front': str(front), 'shadow': str(shadow)}
    _seed_state(layout, exit=journal)
    calls = []

    done = auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db', {}, timeout=60,
                                      run=_fake_engine(calls))

    assert calls == [] and done['phase'] == 'complete'
    assert (layout.authority / 'CONTROL').read_bytes() == b'exported'


@pytest.mark.parametrize('origin, expected', [
    ('automatic', 'disabled'), ('unknown', None), ('explicit', None),
])
def test_turning_off_exports_only_automatic_fronts(layout, origin, expected):
    _active_front(layout, origin=origin, request=False)
    calls = []

    result = auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db',
                                        {'TOFU_DB_FASTPATH': 'off'}, timeout=60,
                                        run=_fake_engine(calls))

    assert (result or {}).get('reason') == expected
    assert bool(calls) == (expected is not None)


def test_explicit_selection_is_never_exited_automatically(layout):
    _active_front(layout)
    calls = []
    assert auto.complete_planned_exit(layout.data_dir, '/engine/tofu-db',
                                      {'TOFU_DB_FASTPATH_DIR': '/explicit'}, timeout=60,
                                      run=_fake_engine(calls)) is None
    assert calls == []
