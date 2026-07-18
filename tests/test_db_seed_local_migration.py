"""Tests for the one-time local-primary seed migration.

`lib/database/_bootstrap._seed_local_pgdata_from_legacy` populates an empty
LOCAL pgdata from the legacy FUSE cluster so the primacy flip is a CONSEQUENCE
of a verified seed. These tests pin the owner-mandated invariants WITHOUT a live
PG by monkeypatching the PG primitives (dump / bootstrap / count / quarantine):

  1. Seed source = fresh live dump first, latest nightly dump only as fallback.
  2. Idempotent on pgdata_is_populated(local) — populated local → no-op.
  3. Verify-before-canonical — restored convs must match source, else quarantine.
  4. Two-restart flip — seed populates local; NEXT resolve returns local.
"""
import os

import pytest

import lib.database._pg_seed as boot  # seed fns relocated (Decoupling D sub-cut 2); patch canonical module
import lib.database.db_paths as db_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ('TOFU_DB_SEED_LOCAL', 'TOFU_DB_LOCAL_SPLIT', 'TOFU_DB_LOCAL_ROOT',
              'TOFU_DB_BACKUP_ROOT'):
        monkeypatch.delenv(v, raising=False)
    yield


def _wire(monkeypatch, *, live_ok, src_convs, restored_convs, bootstrap_ok=True,
          legacy_startable=True, opt_in=True):
    """Monkeypatch the PG primitives; record what the seed did in `calls`.

    live_ok: whether _dump_live_cluster produces a dump (given legacy is up).
    legacy_startable: whether _ensure_legacy_up_for_seed can bring legacy up.
    opt_in: sets TOFU_DB_SEED_LOCAL=1 (the seed is opt-in / default-off).
    """
    if opt_in:
        monkeypatch.setenv('TOFU_DB_SEED_LOCAL', '1')
    calls = {'dump_live': 0, 'nightly_used': False, 'bootstrap': 0,
             'quarantined': [], 'stopped': [], 'legacy_up': 0}

    def _fake_legacy_up(legacy_pgdata, base_dir, user):
        calls['legacy_up'] += 1
        return 5432 if legacy_startable else None
    monkeypatch.setattr(boot, '_ensure_legacy_up_for_seed', _fake_legacy_up)

    def _fake_dump_live(host, port, user, out_path):
        calls['dump_live'] += 1
        if live_ok:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, 'w') as f:
                f.write('-- live dump\n')
            return True
        return False

    def _fake_latest(base_dir):
        # A nightly is available whenever the live path won't be taken: either
        # the live dump fails, or legacy can't be started.
        return ('/fake/pg_backups/pg_dumpall_nightly.sql'
                if (not live_ok or not legacy_startable) else None)

    def _fake_copy(src, dst):
        calls['nightly_used'] = True
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, 'w') as f:
            f.write('-- nightly\n')

    def _fake_bootstrap(pgdata, base_dir, host, port, user, pw, db):
        calls['bootstrap'] += 1
        if not bootstrap_ok:
            return None
        os.makedirs(pgdata, exist_ok=True)
        with open(os.path.join(pgdata, 'PG_VERSION'), 'w') as f:
            f.write('16\n')
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15499,
                'PG_DSN': 'host=127.0.0.1 port=15499'}

    def _fake_count(host, port, user, db):
        # legacy port branch vs local port branch: legacy uses the source count,
        # the seeded local uses restored_convs.
        return src_convs if port == 5432 else restored_convs

    def _fake_read_port(pgdata):
        return 5432  # legacy reachable

    def _fake_quarantine(pgdata):
        calls['quarantined'].append(pgdata)
        dst = pgdata + '.corrupt'
        try:
            os.rename(pgdata, dst)  # mirror the real move-aside
        except OSError:
            pass
        return dst

    def _fake_stop(pgdata):
        calls['stopped'].append(pgdata)

    monkeypatch.setattr(boot, '_dump_live_cluster', _fake_dump_live)
    monkeypatch.setattr(boot, '_latest_pg_backup', _fake_latest)
    monkeypatch.setattr(boot.shutil, 'copy2', _fake_copy)
    monkeypatch.setattr(boot, '_bootstrap_pg', _fake_bootstrap)
    monkeypatch.setattr(boot, '_count_convs', _fake_count)
    monkeypatch.setattr(boot, '_read_our_pg_port', _fake_read_port)
    monkeypatch.setattr(boot, '_quarantine_corrupt_pgdata', _fake_quarantine)
    monkeypatch.setattr(boot, '_boot_stop_pg_quietly', _fake_stop)
    monkeypatch.setattr(boot, '_pg_binaries_present', lambda: True)
    return calls


def _seed(tmp_path):
    base = str(tmp_path)
    local = str(tmp_path / 'local' / 'pgdata')
    legacy = str(tmp_path / 'data' / 'pgdata')
    return boot._seed_local_pgdata_from_legacy(
        local, legacy, base, 15432, 'u', '', 'tofu'), local, legacy


def test_prefers_live_dump(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100)
    ok, local, _ = _seed(tmp_path)
    assert ok is True
    assert calls['dump_live'] == 1
    assert calls['nightly_used'] is False   # live succeeded → nightly untouched
    assert db_paths.pgdata_is_populated(local)


def test_falls_back_to_nightly_when_live_unreachable(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, live_ok=False, src_convs=None, restored_convs=50)
    ok, _, _ = _seed(tmp_path)
    assert ok is True
    assert calls['dump_live'] == 1          # attempted live first
    assert calls['nightly_used'] is True    # then fell back


def test_idempotent_skip_when_local_already_populated(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100)
    local = str(tmp_path / 'local' / 'pgdata')
    os.makedirs(local, exist_ok=True)
    with open(os.path.join(local, 'PG_VERSION'), 'w') as f:
        f.write('16\n')  # pre-populated
    ok = boot._seed_local_pgdata_from_legacy(
        local, str(tmp_path / 'data' / 'pgdata'), str(tmp_path), 15432, 'u', '', 'tofu')
    assert ok is True
    assert calls['dump_live'] == 0          # NEVER dumped
    assert calls['bootstrap'] == 0          # NEVER re-restored over newer local


def test_verify_mismatch_quarantines_and_keeps_legacy(tmp_path, monkeypatch):
    """Restored count != source → quarantine local, return False (legacy canonical)."""
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=3)
    ok, local, _ = _seed(tmp_path)
    assert ok is False
    assert calls['quarantined'] == [local]  # half-restored local moved aside
    assert not db_paths.pgdata_is_populated(local)  # can't satisfy the gate


def test_bootstrap_failure_quarantines(tmp_path, monkeypatch):
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100,
                  bootstrap_ok=False)
    ok, local, _ = _seed(tmp_path)
    assert ok is False
    assert calls['quarantined'] == [local]


def test_opt_in_required_default_off(tmp_path, monkeypatch):
    """Seed is opt-in: without TOFU_DB_SEED_LOCAL=1 it is a total no-op."""
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100,
                  opt_in=False)  # do NOT set the opt-in flag
    ok, _, _ = _seed(tmp_path)
    assert ok is False
    assert calls['dump_live'] == 0
    assert calls['legacy_up'] == 0


def test_legacy_down_but_startable_uses_fresh_live_dump(tmp_path, monkeypatch):
    """THE ordering fix: legacy DOWN at seed time but startable → the seed brings
    it up and takes a FRESH live dump, NOT the stale nightly. (Clean/PARK restart
    is exactly this case.)"""
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100,
                  legacy_startable=True)
    ok, _, _ = _seed(tmp_path)
    assert ok is True
    assert calls['legacy_up'] == 1          # brought legacy up first
    assert calls['dump_live'] == 1          # fresh live dump
    assert calls['nightly_used'] is False   # nightly NOT used


def test_legacy_unstartable_falls_back_to_nightly(tmp_path, monkeypatch):
    """Only when legacy genuinely cannot start do we accept the nightly dump."""
    calls = _wire(monkeypatch, live_ok=True, src_convs=None, restored_convs=50,
                  legacy_startable=False)
    ok, _, _ = _seed(tmp_path)
    assert ok is True
    assert calls['legacy_up'] == 1
    assert calls['dump_live'] == 0          # never dumped a down cluster
    assert calls['nightly_used'] is True


def test_two_restart_flip(tmp_path, monkeypatch):
    """boot-1: seed populates local; boot-2: resolve returns local (the dance)."""
    _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100)
    data_dir = str(tmp_path / 'data')
    # legacy populated (history exists)
    os.makedirs(os.path.join(data_dir, 'pgdata'), exist_ok=True)
    with open(os.path.join(data_dir, 'pgdata', 'PG_VERSION'), 'w') as f:
        f.write('16\n')
    local_root = str(tmp_path / 'local')
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)

    # boot-1: local empty → gate keeps resolution on legacy
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(data_dir, 'pgdata')
    # run the seed (populates local)
    ok = boot._seed_local_pgdata_from_legacy(
        os.path.join(local_root, 'pgdata'), os.path.join(data_dir, 'pgdata'),
        str(tmp_path), 15432, 'u', '', 'tofu')
    assert ok is True
    # boot-2: local now populated → resolution flips to local
    assert db_paths.resolve_pgdata_dir(data_dir) == os.path.join(local_root, 'pgdata')
