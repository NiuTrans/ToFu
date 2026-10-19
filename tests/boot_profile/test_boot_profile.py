"""Boot-profile contract: the restart bar budgets from THIS host's own boot shape.

The in-app restart progress bar cannot stream the booting process's lifespan
over the wire (no server is listening mid-re-exec), so it estimates progress
from a per-phase budget. A hardcoded budget is fiction on any host whose
storage/filesystem profile differs from the author's — that is exactly why the
bar pinned at 96% while the backend was still at phase 3/9 on a network
filesystem. The production lifespan now MEASURES each required phase
(``lib/production_lifecycle`` → ``record_boot_phases``), persists it under the
process data dir, and ``/api/health`` re-exposes the previous boot's profile
(``bootProfile``) so the NEXT restart budgets the bar from the host's own
observed boot shape instead of a universal guess.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


_PHASES = [
    {'key': 'frontend_assets', 'label': 'Frontend assets', 'index': 1,
     'duration_s': 0.2},
    {'key': 'storage_boundary', 'label': 'Exclusive storage boundary',
     'index': 2, 'duration_s': 2.4},
    {'key': 'storage_authority_sql', 'label': 'Storage sidecar', 'index': 3,
     'duration_s': 10.2},
]


def test_record_and_load_round_trip(tmp_path, monkeypatch):
    import lib.boot_profile as bp

    monkeypatch.setattr(bp, '_profile_path',
                        lambda: str(tmp_path / 'boot_profile.json'))

    written = bp.record_boot_phases(
        _PHASES, phase_total=9, boot_to_ready_s=34.5)
    assert written is not None
    assert written['schema'] == 1
    assert written['phase_total'] == 9
    assert written['boot_to_ready_s'] == 34.5
    assert [p['key'] for p in written['phases']] == [
        'frontend_assets', 'storage_boundary', 'storage_authority_sql']
    assert written['phases'][2]['duration_s'] == 10.2

    loaded = bp.load_boot_profile()
    assert loaded == written


def test_load_returns_none_when_absent(tmp_path, monkeypatch):
    import lib.boot_profile as bp

    monkeypatch.setattr(bp, '_profile_path',
                        lambda: str(tmp_path / 'never_written.json'))
    assert bp.load_boot_profile() is None


def test_load_returns_none_on_schema_mismatch(tmp_path, monkeypatch):
    import lib.boot_profile as bp
    from lib.json_store import write_json_atomic

    path = str(tmp_path / 'boot_profile.json')
    monkeypatch.setattr(bp, '_profile_path', lambda: path)
    write_json_atomic(path, {'schema': 999, 'phases': []})
    assert bp.load_boot_profile() is None


def test_record_never_raises_on_bad_entries(tmp_path, monkeypatch):
    """Telemetry must not gate readiness: malformed phase entries are dropped,
    and a persistence failure returns None instead of raising."""
    import lib.boot_profile as bp

    monkeypatch.setattr(bp, '_profile_path',
                        lambda: str(tmp_path / 'boot_profile.json'))
    written = bp.record_boot_phases(
        [{'key': 'ok', 'label': 'L', 'index': 1, 'duration_s': 1.0},
         # duration_s whose float() raises ValueError → entry dropped, not fatal
         {'key': 'bad', 'label': 'L', 'index': 2,
          'duration_s': object()},
         {'key': 'ok2', 'label': 'L2', 'index': 3, 'duration_s': 2.5}],
        phase_total=9, boot_to_ready_s=5.0)
    assert written is not None
    # The un-coercible entry is dropped; the well-formed ones survive in order.
    assert [p['key'] for p in written['phases']] == ['ok', 'ok2']


def test_health_payload_field_shape_matches_frontend(tmp_path, monkeypatch):
    """The frontend reads ``bootProfile.phases[].index`` / ``duration_s`` to
    build its timeline. Assert the persisted profile carries exactly those
    keys so a schema drift breaks the build here, not in the browser."""
    import lib.boot_profile as bp

    monkeypatch.setattr(bp, '_profile_path',
                        lambda: str(tmp_path / 'boot_profile.json'))
    bp.record_boot_phases(_PHASES, phase_total=9, boot_to_ready_s=34.5)
    profile = bp.load_boot_profile()

    assert isinstance(profile['phases'], list) and profile['phases']
    for phase in profile['phases']:
        assert isinstance(phase['index'], int)
        assert isinstance(phase['duration_s'], (int, float))
        assert isinstance(phase['key'], str)
