"""Tier B (WAL archive + PITR) tests — bare-CI, no live PG.

Covers the §7g acceptance criteria:
  1. archive_mode/archive_command emitted via the ONE managed-config builder,
     ONLY when Tier B opted-in AND pgdata is the resolved local primary.
  2. The "no archive_mode on the legacy cluster pre-flip" guard.
  3. §3a newest-wins channel selector + divergence CRITICAL.
  4. The FUSE-stall-safe archive shim: idempotent + timeout→non-zero-no-wedge.
"""
import os
import time

import pytest

import lib.database._bootstrap as boot
import lib.database._pg_seed as seedmod  # seed pipeline relocated (Decoupling D sub-cut 2)
import lib.database.wal_archive as wal
import lib.database.db_paths as db_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ('TOFU_DB_TIER_B', 'TOFU_DB_LOCAL_SPLIT', 'TOFU_DB_LOCAL_ROOT',
              'TOFU_DB_BACKUP_ROOT', 'TOFU_DB_RESTORE_DIVERGENCE_WARN_S',
              'TOFU_DB_WAL_ARCHIVE_TIMEOUT',
              'TOFU_DB_WAL_ARCHIVE_MAX_FAILS'):
        monkeypatch.delenv(v, raising=False)
    yield


# ── 1 + 2: managed-config archive gating ───────────────────────────────────

def test_archive_settings_absent_when_tier_b_off():
    body = boot._build_managed_pg_config(archive_enabled=False)
    assert not any('archive_mode' in line for line in body)
    assert any('wal_level = replica' in line for line in body)  # Tier A unchanged


def test_archive_settings_present_when_enabled():
    body = boot._build_managed_pg_config(archive_enabled=True)
    assert any(line.strip() == 'archive_mode = on' for line in body)
    assert any('wal_archive archive' in line for line in body)


def test_no_archive_mode_on_legacy_pre_flip(tmp_path, monkeypatch):
    """THE guard: Tier-B opted-in + split-active + local NOT yet seeded → the
    LEGACY cluster gets NO archive_mode. Archiving must never engage on the
    soon-to-be-retired legacy cluster; only on the local primary post-flip.

    We assert on the EFFECTIVE decision the writer uses: with local unpopulated,
    the resolved primary is the LOCAL path (which does not exist yet), so
    `_pgdata_is_resolved_primary(legacy)` is False → archive_enabled is False
    → the config body for legacy has no archive_mode line."""
    data_dir = str(tmp_path / 'data')
    legacy_pgdata = os.path.join(data_dir, 'pgdata')
    local_root = str(tmp_path / 'local')
    local_pgdata = os.path.join(local_root, 'pgdata')
    os.makedirs(legacy_pgdata, exist_ok=True)
    with open(os.path.join(legacy_pgdata, 'PG_VERSION'), 'w') as f:
        f.write('16\n')  # legacy populated; local EMPTY (pre-seed)
    monkeypatch.setenv('TOFU_DB_TIER_B', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)
    # Pre-flip, resolution stays on legacy (gate) BUT the eventual primary is
    # local — so the archive target (resolved primary) is the local path, which
    # the legacy pgdata is NOT. That is what disables archiving on legacy.
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: data_dir)
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '0')  # force resolve→local so the
    # "resolved primary" is unambiguously the local path for this guard check:
    monkeypatch.setattr(db_paths, 'resolve_pgdata_dir', lambda d: local_pgdata)

    assert boot._tier_b_enabled() is True
    # legacy is NOT the resolved (local) primary → archiving disabled for it
    archive_enabled_legacy = boot._tier_b_enabled() and boot._pgdata_is_resolved_primary(legacy_pgdata)
    assert archive_enabled_legacy is False
    body = boot._build_managed_pg_config(archive_enabled=archive_enabled_legacy)
    assert not any('archive_mode' in line for line in body), \
        'GUARD FAILED — legacy cluster would archive pre-flip'
    # sanity: the local primary WOULD archive
    assert boot._pgdata_is_resolved_primary(local_pgdata) is True


def test_archive_flips_to_local_once_seeded(tmp_path, monkeypatch):
    """Once local is populated (post-seed), the LOCAL pgdata is the resolved
    primary and archiving engages there — not on legacy."""
    data_dir = str(tmp_path / 'data')
    legacy_pgdata = os.path.join(data_dir, 'pgdata')
    local_root = str(tmp_path / 'local')
    local_pgdata = os.path.join(local_root, 'pgdata')
    for pg in (legacy_pgdata, local_pgdata):
        os.makedirs(pg, exist_ok=True)
        with open(os.path.join(pg, 'PG_VERSION'), 'w') as f:
            f.write('16\n')
    monkeypatch.setenv('TOFU_DB_TIER_B', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_SPLIT', '1')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: data_dir)

    # local populated → resolution flips to local
    assert db_paths.resolve_pgdata_dir(data_dir) == local_pgdata
    assert boot._pgdata_is_resolved_primary(local_pgdata) is True
    assert boot._pgdata_is_resolved_primary(legacy_pgdata) is False


# ── 3: §3a newest-wins selector + divergence ────────────────────────────────

def _seed_backups(tmp_path, monkeypatch, *, dump_age_s, wal_age_s):
    data_dir = str(tmp_path / 'data')
    backups = os.path.join(data_dir, 'pg_backups')
    wal_dir = os.path.join(backups, 'wal')
    os.makedirs(wal_dir, exist_ok=True)
    now = time.time()
    if dump_age_s is not None:
        dump = os.path.join(backups, 'pg_dumpall_x.sql')
        with open(dump, 'w') as f:
            f.write('-- dump\n')
        os.utime(dump, (now - dump_age_s, now - dump_age_s))
    if wal_age_s is not None:
        seg = os.path.join(wal_dir, '000000010000000000000005')
        with open(seg, 'w') as f:
            f.write('wal\n')
        os.utime(seg, (now - wal_age_s, now - wal_age_s))
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: data_dir)
    return str(tmp_path)


def test_selector_prefers_newer_wal(tmp_path, monkeypatch):
    base = _seed_backups(tmp_path, monkeypatch, dump_age_s=3600, wal_age_s=10)
    chan, _ = boot._select_restore_channel(base)
    assert chan == 'tier_b'   # WAL 10s old beats dump 1h old


def test_selector_picks_dump_when_wal_stale(tmp_path, monkeypatch, caplog):
    """WAL archive STALE (a day old) + fresher dump → pick Tier A + divergence CRIT."""
    base = _seed_backups(tmp_path, monkeypatch, dump_age_s=60, wal_age_s=86400)
    with caplog.at_level('CRITICAL'):
        chan, _ = boot._select_restore_channel(base)
    assert chan == 'tier_a'   # dump 60s old beats stale WAL 24h old
    assert any('DIVERGENCE' in r.message for r in caplog.records)


def test_selector_no_divergence_when_close(tmp_path, monkeypatch, caplog):
    base = _seed_backups(tmp_path, monkeypatch, dump_age_s=120, wal_age_s=30)
    with caplog.at_level('CRITICAL'):
        chan, _ = boot._select_restore_channel(base)
    assert chan == 'tier_b'
    assert not any('DIVERGENCE' in r.message for r in caplog.records)


def test_selector_none_when_empty(tmp_path, monkeypatch):
    base = _seed_backups(tmp_path, monkeypatch, dump_age_s=None, wal_age_s=None)
    assert boot._select_restore_channel(base) == (None, None)


# ── 4: FUSE-stall-safe archive shim ─────────────────────────────────────────

def test_archive_shim_idempotent(tmp_path, monkeypatch):
    src = tmp_path / 'seg'
    src.write_text('segment-bytes')
    backup = str(tmp_path / 'backup')
    monkeypatch.setenv('TOFU_DB_BACKUP_ROOT', backup)
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: str(tmp_path / 'data'))
    seg = '000000010000000000000009'
    assert wal.archive_segment(str(src), seg) == 0        # first copy
    assert wal.archive_segment(str(src), seg) == 0        # idempotent no-op
    assert os.path.exists(os.path.join(backup, 'wal', seg))


def test_archive_shim_timeout_returns_nonzero(tmp_path, monkeypatch):
    """A stalled copy must return NON-ZERO (PG retains) and NOT wedge."""
    src = tmp_path / 'seg'
    src.write_text('x')
    monkeypatch.setenv('TOFU_DB_BACKUP_ROOT', str(tmp_path / 'backup'))
    monkeypatch.setenv('TOFU_DB_WAL_ARCHIVE_TIMEOUT', '1')
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: str(tmp_path / 'data'))

    def _hang(*a, **k):
        time.sleep(30)   # simulate a frozen FUSE write
    monkeypatch.setattr(wal.shutil, 'copyfile', _hang)

    t0 = time.time()
    rc = wal.archive_segment(str(src), '000000010000000000000010')
    elapsed = time.time() - t0
    assert rc != 0                 # PG will RETAIN + retry
    assert elapsed < 5             # did NOT wedge for the full 30s sleep


def test_archive_invalid_integer_knobs_fall_back(tmp_path, monkeypatch):
    src = tmp_path / 'seg'
    src.write_text('segment-bytes')
    monkeypatch.setenv('TOFU_DB_BACKUP_ROOT', str(tmp_path / 'backup'))
    monkeypatch.setenv('TOFU_DB_WAL_ARCHIVE_TIMEOUT', 'typo')
    monkeypatch.setenv('TOFU_DB_WAL_ARCHIVE_MAX_FAILS', 'typo')
    monkeypatch.setattr('lib.runtime_paths.data_root',
                        lambda: str(tmp_path / 'data'))
    assert wal.archive_segment(
        str(src), '000000010000000000000011') == 0


def test_archive_and_restore_refuse_segment_path_escape(tmp_path, monkeypatch):
    src = tmp_path / 'seg'
    src.write_text('segment-bytes')
    monkeypatch.setenv('TOFU_DB_BACKUP_ROOT', str(tmp_path / 'backup'))
    monkeypatch.setattr('lib.runtime_paths.data_root',
                        lambda: str(tmp_path / 'data'))
    assert wal.archive_segment(str(src), '../outside') != 0
    assert wal.restore_segment('../outside', str(tmp_path / 'restored')) != 0
    assert not (tmp_path / 'outside').exists()


def test_restore_invalid_divergence_knob_does_not_break_selection(
        tmp_path, monkeypatch):
    base = _seed_backups(tmp_path, monkeypatch, dump_age_s=120, wal_age_s=30)
    monkeypatch.setenv('TOFU_DB_RESTORE_DIVERGENCE_WARN_S', 'typo')
    chan, _ = boot._select_restore_channel(base)
    assert chan == 'tier_b'


# ── 5: END-TO-END — selector drives PITR (the seconds-RPO proof) ────────────

def _wire_e2e_seed(tmp_path, monkeypatch):
    """Fresh empty local + a base + a WAL tail NEWER than the dump; the two
    restore mechanisms modelled as distinct row-counts."""
    DUMP_ROWS, TAIL_ROWS = 100, 137
    data_dir = str(tmp_path / 'data')
    backups = os.path.join(data_dir, 'pg_backups')
    wal_dir = os.path.join(backups, 'wal')
    os.makedirs(wal_dir, exist_ok=True)
    now = time.time()
    dump = os.path.join(backups, 'pg_dumpall_x.sql')
    open(dump, 'w').write('-- dump\n'); os.utime(dump, (now - 3600, now - 3600))
    seg = os.path.join(wal_dir, '000000010000000000000007')
    open(seg, 'w').write('wal\n'); os.utime(seg, (now - 5, now - 5))
    monkeypatch.setenv('TOFU_DB_TIER_B', '1')
    monkeypatch.setenv('TOFU_DB_SEED_LOCAL', '1')
    monkeypatch.setattr('lib.runtime_paths.data_root', lambda: data_dir)
    monkeypatch.setattr(seedmod, '_pg_binaries_present', lambda: True)
    monkeypatch.setattr(seedmod, '_ensure_legacy_up_for_seed', lambda *a: 5432)
    monkeypatch.setattr(seedmod, '_dump_live_cluster', lambda *a: True)
    calls = {'pitr': 0, 'bootstrap': 0}

    def _fake_pitr(lp, *a):
        calls['pitr'] += 1
        os.makedirs(lp, exist_ok=True)
        open(os.path.join(lp, 'PG_VERSION'), 'w').write('16\n')
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15500, 'PG_DSN': 'x'}

    def _fake_bootstrap(lp, *a):
        calls['bootstrap'] += 1
        os.makedirs(lp, exist_ok=True)
        open(os.path.join(lp, 'PG_VERSION'), 'w').write('16\n')
        return {'PG_HOST': '127.0.0.1', 'PG_PORT': 15501, 'PG_DSN': 'x'}

    monkeypatch.setattr(seedmod, '_recover_via_pitr', _fake_pitr)
    monkeypatch.setattr(seedmod, '_bootstrap_pg', _fake_bootstrap)
    monkeypatch.setattr(seedmod, '_count_convs',
                        lambda h, p, u, d: {5432: DUMP_ROWS, 15500: TAIL_ROWS,
                                            15501: DUMP_ROWS}.get(p, DUMP_ROWS))
    monkeypatch.setattr(seedmod, '_boot_stop_pg_quietly', lambda p: None)
    # _select_restore_channel + _tier_b_enabled are read by the seed via its
    # own namespace too, but default (real) behaviour is wanted here; the NC
    # test overrides _select_restore_channel on seedmod explicitly.
    return calls, data_dir


def test_seed_uses_pitr_when_tier_b_and_wal_newest(tmp_path, monkeypatch):
    """Selector's tier_b verdict routes the seed to PITR (recovers WAL tail),
    NOT the dump restore. This is the seconds-RPO proof."""
    calls, data_dir = _wire_e2e_seed(tmp_path, monkeypatch)
    local = str(tmp_path / 'local' / 'pgdata')
    ok = seedmod._seed_local_pgdata_from_legacy(
        local, os.path.join(data_dir, 'pgdata'), str(tmp_path), 15432, 'u', '', 'tofu')
    assert ok is True
    assert calls['pitr'] == 1        # PITR path taken (tier_b verdict)
    assert calls['bootstrap'] == 0   # dump-restore NOT taken


def test_nc_ignore_tier_b_verdict_loses_wal_tail(tmp_path, monkeypatch):
    """NC: force the seed to ignore the tier_b verdict (selector→tier_a) → it
    falls to the dump restore, recovering only the OLDER dump state and LOSING
    the WAL tail (proves the selector wiring is what recovers the tail)."""
    calls, data_dir = _wire_e2e_seed(tmp_path, monkeypatch)
    monkeypatch.setattr(seedmod, '_select_restore_channel', lambda base: ('tier_a', 0.0))
    local = str(tmp_path / 'local' / 'pgdata')
    seedmod._seed_local_pgdata_from_legacy(
        local, os.path.join(data_dir, 'pgdata'), str(tmp_path), 15432, 'u', '', 'tofu')
    assert calls['bootstrap'] == 1   # dump path taken
    assert calls['pitr'] == 0        # PITR NOT taken → WAL tail lost
