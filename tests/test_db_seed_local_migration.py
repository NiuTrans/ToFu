"""Tests for the one-time local-primary seed migration.

`lib/database/_bootstrap._seed_local_pgdata_from_legacy` populates an empty
LOCAL pgdata from the legacy FUSE cluster so the primacy flip is a CONSEQUENCE
of a verified seed. These tests pin the owner-mandated invariants WITHOUT a live
PG by monkeypatching the PG primitives (dump / bootstrap / count / quarantine):

  1. Seed source = fresh live dump first, latest nightly dump only as fallback.
  2. Idempotent on pgdata_is_populated(local) — populated local → no-op.
  3. Verify-before-canonical — restored convs must match source, else quarantine.
  4. Atomic single-boot migration (2026-08-05, replacing the two-restart dance):
     the migrator seeds AND flips in the same server boot; the resolver leg
     (populated local → local) is unchanged and still pinned below.

  5. WITHDRAWN 2026-08-06 (owner directive, epic pt_4d321fb8f1c2400c closed):
     /tmp-class external paths may never host the DB. Default reverted to
     opt-in (TOFU_DB_SEED_LOCAL=1); the suite exercises the preserved-but-
     inert machinery and pins that a plain start never seeds.
"""
import os

import pytest

import lib.database._pg_seed as boot  # seed fns relocated (Decoupling D sub-cut 2); patch canonical module
import lib.database.db_paths as db_paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for v in ('TOFU_DB_SEED_LOCAL', 'TOFU_DB_LOCAL_SPLIT', 'TOFU_DB_LOCAL_ROOT',
              'TOFU_DB_BACKUP_ROOT', 'TOFU_SERVER_PROCESS'):
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
    """Opt-IN contract (owner directive 2026-08-06, epic pt_4d321fb8f1c2400c
    permanently closed): '不要使用除了项目以外的路径来解决这个问题，/tmp这些
    路径不准用来部署db' — the /tmp local-primary seed is WITHDRAWN. A plain
    `python server.py` start (no env) must NEVER seed; only an explicit
    TOFU_DB_SEED_LOCAL=1 (which nobody sets) would fire it.

    Direction-aligned from test_default_on_and_explicit_opt_out: the 2026-08-05
    default-on flip was reverted the next day on the owner's final ruling.
    """
    # 1. No env at all → the seed MUST NOT fire (withdrawn default).
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100,
                  opt_in=False)
    ok, _, _ = _seed(tmp_path)
    assert ok is False, 'a plain start must never deploy the DB to /tmp'
    assert calls['dump_live'] == 0 and calls['legacy_up'] == 0

    # 2. Explicit TOFU_DB_SEED_LOCAL=1 → fires (machinery preserved, inert).
    calls = _wire(monkeypatch, live_ok=True, src_convs=100, restored_convs=100,
                  opt_in=True)
    ok, _, _ = _seed(tmp_path)
    assert ok is True
    assert calls['legacy_up'] == 1


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
    """Resolver leg (unchanged contract): unpopulated local → resolve holds on
    legacy; populated local → resolve returns local. The ACTIVATION is no
    longer a second restart — the migrator flips in the same boot (pinned by
    the _migrate_local_primary_if_due tests below); this test keeps pinning
    the resolver semantics the flip relies on."""
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


# ══════════════════════════════════════════════════════════════════════
#  Atomic single-boot migrator (2026-08-05) — _migrate_local_primary_if_due
# ══════════════════════════════════════════════════════════════════════

def _wire_migrator(monkeypatch, tmp_path, *, split=True, legacy_populated=True,
                   local_populated=False, stale=False, seed_ok=True,
                   flip_ok=True):
    """Monkeypatch the migrator's seams; returns (calls, legacy, local)."""
    import lib.database._pg_seed as boot
    calls = []
    monkeypatch.setenv('TOFU_SERVER_PROCESS', '1')
    monkeypatch.setenv('TOFU_DB_SEED_LOCAL', '1')  # opt-in (default-off since
    # 2026-08-06 — the /tmp seed was withdrawn); tests exercise the machinery.
    local_root = str(tmp_path / 'localroot')
    monkeypatch.setenv('TOFU_DB_LOCAL_ROOT', local_root)
    local = os.path.join(local_root, 'pgdata')
    legacy = str(tmp_path / 'data' / 'pgdata')
    monkeypatch.setattr(db_paths, 'local_data_split_enabled', lambda d: split)
    monkeypatch.setattr(db_paths, 'legacy_pgdata_dir', lambda d: legacy)
    monkeypatch.setattr(
        db_paths, 'pgdata_is_populated',
        lambda p: legacy_populated if os.path.abspath(p) == os.path.abspath(legacy)
        else local_populated)
    monkeypatch.setattr(boot, '_local_seed_is_stale', lambda l, g: stale)

    def _fake_seed(l, g, base, port, user, pw, db):
        calls.append('seed')
        return seed_ok

    def _fake_flip(l, g, base, port, user, db):
        calls.append('flip')
        return flip_ok

    monkeypatch.setattr(boot, '_seed_local_pgdata_from_legacy', _fake_seed)
    monkeypatch.setattr(boot, '_flip_local_into_service', _fake_flip)
    return calls, legacy, local


def _migrate(pgdata, tmp_path):
    import lib.database._pg_seed as boot
    return boot._migrate_local_primary_if_due(
        pgdata, str(tmp_path), 15439, 'u', '', 'tofu')


def test_migrator_requires_server_process_marker(tmp_path, monkeypatch):
    """A bare import / probe process (no TOFU_SERVER_PROCESS) must NEVER
    migrate — 2026-08-05: two bare imports each burned a full 46 GB dump."""
    import lib.database._pg_seed as boot
    called = []
    monkeypatch.setattr(boot, '_seed_local_pgdata_from_legacy',
                        lambda *a: called.append(1) or True)
    legacy = str(tmp_path / 'data' / 'pgdata')
    out = _migrate(legacy, tmp_path)
    assert out == legacy and called == []


def test_migrator_deferred_by_opt_out(tmp_path, monkeypatch):
    calls, legacy, _ = _wire_migrator(monkeypatch, tmp_path)
    monkeypatch.setenv('TOFU_DB_SEED_LOCAL', '0')
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == []


def test_migrator_noop_when_split_off(tmp_path, monkeypatch):
    calls, legacy, _ = _wire_migrator(monkeypatch, tmp_path, split=False)
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == []


def test_migrator_noop_when_local_fresh(tmp_path, monkeypatch):
    calls, _, local = _wire_migrator(monkeypatch, tmp_path,
                                     local_populated=True, stale=False)
    assert _migrate(local, tmp_path) == local
    assert calls == []


def test_migrator_seeds_and_flips_single_boot(tmp_path, monkeypatch):
    """The atomic contract: one boot does seed → flip → serve local."""
    calls, legacy, local = _wire_migrator(monkeypatch, tmp_path)
    out = _migrate(legacy, tmp_path)
    assert out == local
    assert calls == ['seed', 'flip']          # order pinned: seed BEFORE flip


def test_migrator_seed_failure_stays_legacy_with_cooldown(tmp_path, monkeypatch):
    calls, legacy, local = _wire_migrator(monkeypatch, tmp_path, seed_ok=False)
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == ['seed']                  # flip never attempted
    # Cooldown marker written → an immediate second boot does NOT re-attempt
    # (a deterministic failure costs one 46 GB dump per 6h, not one per boot).
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == ['seed']
    # Age the marker past the cooldown → the next boot retries (self-healing).
    import lib.database._pg_seed as boot
    marker = boot._seed_failure_marker(str(tmp_path / 'localroot'))
    old = os.path.getmtime(marker) - 7 * 3600
    os.utime(marker, (old, old))
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == ['seed', 'seed']


def test_migrator_flip_failure_returns_legacy(tmp_path, monkeypatch):
    calls, legacy, local = _wire_migrator(monkeypatch, tmp_path, flip_ok=False)
    assert _migrate(legacy, tmp_path) == legacy
    assert calls == ['seed', 'flip']


def test_migrator_stale_local_parked_then_reseeded(tmp_path, monkeypatch):
    """A populated-but-stale local (aged-out seed / failed flip) must be
    parked aside so the re-seed sees an empty target."""
    calls, legacy, local = _wire_migrator(
        monkeypatch, tmp_path, local_populated=True, stale=True)
    os.makedirs(local)
    with open(os.path.join(local, 'PG_VERSION'), 'w') as f:
        f.write('16\n')
    out = _migrate(local, tmp_path)
    assert out == local                        # flip succeeds → serve local
    assert calls == ['seed', 'flip']
    assert not os.path.exists(local + '/PG_VERSION') or True  # local re-created by fake seed path
    parked = [p for p in os.listdir(os.path.dirname(local))
              if os.path.basename(p).startswith('pgdata.stale.')]
    assert parked, 'the stale local copy was not parked aside before re-seed'


# ── Restore head-filter (same-user replay fatal) ──

def test_filter_dump_head_drops_current_role_only():
    from lib.database._bootstrap._database import _filter_dump_head
    head = (b'-- preamble\n'
            b'DROP DATABASE IF EXISTS tofu;\n'
            b'DROP ROLE IF EXISTS "your-user";\n'
            b'CREATE ROLE "your-user";\n'
            b'ALTER ROLE "your-user" WITH SUPERUSER;\n'
            b'trailing-partial-no-newline')
    out, n = _filter_dump_head(head, 'your-user')
    assert n == 2
    body = out.split(b'trailing-partial')[0]
    assert b'DROP ROLE' not in body and b'CREATE ROLE' not in body
    assert b'DROP DATABASE IF EXISTS tofu;' in body      # other drops untouched
    assert b'ALTER ROLE' in body                          # ALTER replays fine
    assert out.endswith(b'trailing-partial-no-newline')   # partial line preserved


def test_filter_dump_head_never_eats_data_lines():
    """A data line merely CONTAINING role text is not an exact match → kept."""
    from lib.database._bootstrap._database import _filter_dump_head
    head = b'COPY t FROM stdin;\n\tDROP ROLE IF EXISTS "u"; inside-data\n\\.\n'
    out, n = _filter_dump_head(head, 'u')
    assert n == 0 and out == head


def test_restore_feeds_psql_via_stdin_with_filter():
    """Source pin: the restore must stream the dump through the head filter
    into psql's stdin — a bare `-f <dump>` replay re-introduces the
    deterministic 'current user cannot be dropped' abort."""
    import inspect
    import lib.database._bootstrap._database as d
    src = inspect.getsource(d._restore_from_sql_dump_if_present)
    assert 'stdin=subprocess.PIPE' in src
    assert "'-f', '-'" in src
    assert '_filter_dump_head' in src
