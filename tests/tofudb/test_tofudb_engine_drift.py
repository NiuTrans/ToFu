"""Deployment drift guard: the pinned engine must speak the current contract.

A content pin proves the deployed binary is the certified artifact; only a
live semantic round-trip proves the artifact still implements the current
application storage contract. These tests pin the guard's verdict semantics
against stale shapes and exercise the full probe against a real engine when
one is resolvable (same availability ladder as the differential suite).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scripts import check_tofudb_engine_drift as guard

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_probe_pins_candidate_and_never_inherits_live_connection(tmp_path, monkeypatch):
    engine = tmp_path / 'tofu-db'
    engine.write_bytes(b'candidate')
    monkeypatch.setenv('TOFUDB_BIN', '/old/engine')
    monkeypatch.setenv('TOFU_STORAGE_CONNECTION_FILE', '/live/locator')
    def inspect(*, startup_timeout):
        assert os.environ['TOFUDB_BIN'] == str(engine)
        assert os.environ['TOFU_STORAGE_CONNECTION_FILE'] == ''
        assert os.environ['TOFUDB_TRANSPORT'] == 'daemon'
        assert os.environ['TOFUDB_EXPECTED_SHA256'] == guard._sha256_file(engine)
        raise RuntimeError('probe failure')
    monkeypatch.setattr(guard, '_probe_selected_engine', inspect)
    with pytest.raises(RuntimeError, match='probe failure'):
        guard._probe_engine(str(engine), startup_timeout=1)
    assert os.environ['TOFUDB_BIN'] == '/old/engine'
    assert os.environ['TOFU_STORAGE_CONNECTION_FILE'] == '/live/locator'


def _resolvable_engine() -> Path | None:
    # Probe the admitted deployment artifact, including its project pin. A
    # stale local debug build is not evidence about the next application boot.
    try:
        return Path(guard.resolve_deployment_binary(REPO_ROOT))
    except RuntimeError as error:
        if (not os.environ.get('TOFUDB_BIN')
                and str(error) == 'TOFUDB_BIN must name an executable tofu-db binary'):
            return None
        raise


@pytest.mark.unit
def test_reclaim_verdict_accepts_the_current_boundary_shape():
    # The current boundary (docs/STORAGE_TOFUDB.md#online-reclaim-boundary):
    # online collector serves reclaim; offline_required is false, no reason code.
    current = {
        'backend': 'tofu-db', 'budget_ms': 10,
        'offline_required': False, 'reason_code': None,
        'statistics_complete': False, 'reclaimed': 0, 'reclaimed_bytes': 0,
        'more_candidates': False, 'epoch_in_progress': False,
        'epochs_completed': 0,
    }
    assert guard.reclaim_boundary_verdict(current) == []


@pytest.mark.unit
@pytest.mark.parametrize('stale', [
    {},  # pre-boundary engine: no deferred-GC fields at all
    {'backend': 'tofu-db', 'budget_ms': 10, 'reclaimed': 0,
     'reclaimed_bytes': 0, 'more_candidates': False},
    {'backend': 'tofu-db', 'budget_ms': 10, 'offline_required': True,
     'reason_code': 'exclusive_gc_required', 'statistics_complete': False,
     'reclaimed': 3, 'reclaimed_bytes': 0, 'more_candidates': False},
    {'backend': 'tofu-db', 'budget_ms': 10, 'offline_required': True,
     'reason_code': 'exclusive_gc_required', 'statistics_complete': False,
     'reclaimed': 0, 'reclaimed_bytes': 0, 'more_candidates': 'no'},
    'offline_required',
])

@pytest.mark.unit
def test_reclaim_verdict_rejects_stale_or_malformed_shapes(stale):
    assert guard.reclaim_boundary_verdict(stale)



@pytest.mark.unit
def test_reclaim_verdict_rejects_sql_authority_answers():
    sqlite_shape = {'backend': 'sqlite', 'reclaimed': 1, 'reclaimed_bytes': 4096,
                    'more_candidates': True, 'budget_ms': 10}
    assert guard.reclaim_boundary_verdict(sqlite_shape)



@pytest.mark.unit
def test_unresolvable_engine_is_a_usage_error(monkeypatch):
    monkeypatch.setattr(guard, 'resolve_deployment_binary',
                        lambda _root: (_ for _ in ()).throw(RuntimeError('no engine')))
    assert guard.main([]) == 2



@pytest.mark.unit
def test_non_executable_explicit_binary_is_a_usage_error(tmp_path):
    candidate = tmp_path / 'tofu-db'
    candidate.write_text('not an engine', encoding='utf-8')
    assert guard.main(['--binary', str(candidate)]) == 2



@pytest.mark.unit
def test_missing_explicit_binary_is_a_usage_error(tmp_path):
    assert guard.main(['--binary', str(tmp_path / 'absent')]) == 2


@pytest.mark.api
def test_current_engine_satisfies_the_probed_contract(capsys):
    engine = _resolvable_engine()
    if engine is None:
        pytest.skip('no tofu-db engine binary resolvable')
    assert guard.main(['--binary', str(engine), '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ok'] is True
    assert report['binary'] == str(engine.resolve())
    assert {probe['probe'] for probe in report['probes']} == {
        'semantic_roundtrip', 'online_reclaim_boundary', 'owner_scoped_write_read'}
    assert all(probe['ok'] for probe in report['probes'])
