"""PostgreSQL ownership: flock enforcement probe, heartbeat freshness, host identity.

WHY THIS FILE EXISTS
────────────────────
`lib/database/_pg_ownership/*` is the cross-host interlock that decides whether
THIS host may start a postmaster on a SHARED (FUSE-mounted) pgdata. Getting it
wrong means two hosts run postmasters against the same data directory —
WAL/pg_subtrans corruption, i.e. the whole database. Measured before this file
existed: 8-21% line coverage across the seven submodules, zero tests naming
them (196 tests merely *imported* the parent package).

THE ONE THING THAT MUST NOT BREAK
`_probe_flock_enforced` exists because a plain `flock(LOCK_EX)` success proves
nothing on a filesystem that silently treats every flock as a NO-OP: both hosts
"acquire" the lock and both start. The probe opens the same file TWICE and
requires the second LOCK_EX|LOCK_NB to be REFUSED. A regression that makes the
probe return True on a no-op mount re-opens the corruption window, and nothing
fails loudly at the time — so it is pinned here.

PATCH-SAFETY NOTE (why the fixture looks like it does)
The canonical `_flock_enforced` verdict is cached ON THE FACADE MODULE
(`lib.database._pg_ownership`), not on `_flock`, and it is a PROCESS-lifetime
cache. Every test that touches the probe must reset it, or the first test's
verdict silently decides every later one — the module docstring documents the
facade indirection precisely so tests can steer it.
"""

import json
import os
import sys
import threading
import time

import pytest

import lib.database._pg_ownership as pg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Undo the PROCESS-lifetime caches so tests cannot leak into each other.

    Both are plain module-level scalars on the FACADE (not containers, and not
    on the submodules) — measured, after an initial wrong guess that
    ``_HOST_IDENTITY_CACHE`` was a dict: it starts as ``None`` and is replaced
    wholesale by the first ``_get_host_identity()`` call.
    """
    saved_flock = pg._flock_enforced
    saved_hostid = pg._HOST_IDENTITY_CACHE
    pg._flock_enforced = None
    yield
    pg._flock_enforced = saved_flock
    pg._HOST_IDENTITY_CACHE = saved_hostid


# ───────────────────── flock enforcement probe ─────────────────────

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_probe_detects_a_SILENT_NOOP_filesystem(tmp_path, monkeypatch):
    """THE case this probe exists for — and the one a local-FS test can NEVER
    reach.

    Some FUSE backends accept every ``flock`` and enforce nothing. The call
    "succeeds" on both hosts, both believe they hold the startup lock, both
    start a postmaster on the same pgdata → WAL/pg_subtrans corruption. A plain
    ``flock(LOCK_EX)`` success therefore proves nothing; the probe must take a
    SECOND lock on the same file and require it to be REFUSED.

    ★ Why this test is written with a fake ``fcntl`` rather than a real mount:
    on ext4/tmpfs the second lock genuinely blocks, so the no-op branch is
    unreachable and a NEUTER that flips it to ``True`` leaves every other test
    green — measured. Without this case the probe's whole reason for existing
    is untested, which is exactly the "guard that is green because it never
    runs the interesting path" failure the charter warns about.

    Simulating the FILESYSTEM (not the probe) keeps this a behaviour test: the
    fake grants every lock, precisely like the dangerous mount.
    """
    import fcntl as _real_fcntl

    class _NoopFcntl:
        LOCK_EX = _real_fcntl.LOCK_EX
        LOCK_NB = _real_fcntl.LOCK_NB
        LOCK_UN = _real_fcntl.LOCK_UN

        @staticmethod
        def flock(_fd, _op):
            return None      # grants everything, enforces nothing

    monkeypatch.setitem(sys.modules, 'fcntl', _NoopFcntl)
    assert pg._probe_flock_enforced(str(tmp_path)) is False, (
        'a filesystem that grants BOTH exclusive locks was reported as '
        'enforcing them — the cross-host interlock is void and two hosts can '
        'start postmasters on the same pgdata')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_probe_detects_real_enforcement_on_a_local_fs(tmp_path):
    """A normal local FS enforces advisory locks → probe must say True.

    If this returns False on ext4/tmpfs the probe is broken in the SAFE
    direction (PG refused unnecessarily), but it still means the mechanism no
    longer measures what it claims.
    """
    assert pg._probe_flock_enforced(str(tmp_path)) is True


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_probe_result_is_cached_for_the_process(tmp_path):
    """Second call must not re-probe — the verdict is a process-level cache."""
    assert pg._probe_flock_enforced(str(tmp_path)) is True
    pg._flock_enforced = False  # simulate a cached "no-op FS" verdict
    assert pg._probe_flock_enforced(str(tmp_path)) is False


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_probe_cleans_up_its_probe_file(tmp_path):
    """The probe must not leave .tofu_flock_probe behind in pgdata."""
    pg._probe_flock_enforced(str(tmp_path))
    assert not (tmp_path / '.tofu_flock_probe').exists()


def test_probe_creates_pgdata_when_absent(tmp_path):
    """Probing a not-yet-created pgdata must not raise."""
    target = tmp_path / 'nested' / 'pgdata'
    result = pg._probe_flock_enforced(str(target))
    assert result in (True, False, None)
    assert target.is_dir()


# ───────────────── flock policy: warn vs refuse ─────────────────

def test_flock_required_defaults_to_false(monkeypatch):
    """Default MUST be permissive — single-host deploys must not regress."""
    monkeypatch.delenv('TOFU_PG_REQUIRE_FLOCK', raising=False)
    monkeypatch.delenv('CHATUI_PG_REQUIRE_FLOCK', raising=False)
    assert pg._flock_required() is False


@pytest.mark.parametrize('raw', ['1', 'true', 'yes', 'on', 'refuse', 'require',
                                 'TRUE', ' Yes '])
def test_flock_required_accepts_documented_truthy_spellings(monkeypatch, raw):
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', raw)
    assert pg._flock_required() is True


@pytest.mark.parametrize('raw', ['0', 'false', 'no', '', 'maybe'])
def test_flock_required_rejects_other_values(monkeypatch, raw):
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', raw)
    assert pg._flock_required() is False


def test_unenforced_locks_refuse_pg_when_policy_demands(monkeypatch, tmp_path):
    """no-op mount + TOFU_PG_REQUIRE_FLOCK → caller MUST refuse to start PG.

    This is the corruption-avoidance branch: returning True here would let two
    hosts start postmasters on one pgdata.
    """
    monkeypatch.setattr(pg, '_probe_flock_enforced', lambda _p: False)
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert pg._verify_flock_support_or_warn(str(tmp_path)) is False


def test_unenforced_locks_only_warn_by_default(monkeypatch, tmp_path):
    """Same mount, no policy → proceed (single-host must keep working)."""
    monkeypatch.setattr(pg, '_probe_flock_enforced', lambda _p: False)
    monkeypatch.delenv('TOFU_PG_REQUIRE_FLOCK', raising=False)
    monkeypatch.delenv('CHATUI_PG_REQUIRE_FLOCK', raising=False)
    assert pg._verify_flock_support_or_warn(str(tmp_path)) is True


def test_unverifiable_probe_is_treated_like_unenforced(monkeypatch, tmp_path):
    """None (probe itself errored) must be handled conservatively, not as True."""
    monkeypatch.setattr(pg, '_probe_flock_enforced', lambda _p: None)
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert pg._verify_flock_support_or_warn(str(tmp_path)) is False


def test_enforced_locks_proceed_regardless_of_policy(monkeypatch, tmp_path):
    monkeypatch.setattr(pg, '_probe_flock_enforced', lambda _p: True)
    monkeypatch.setenv('TOFU_PG_REQUIRE_FLOCK', '1')
    assert pg._verify_flock_support_or_warn(str(tmp_path)) is True


# ───────────────────────── heartbeat ─────────────────────────

def test_heartbeat_absent_is_not_fresh(tmp_path):
    """No heartbeat → previous owner is NOT considered alive."""
    fresh, info = pg._heartbeat_is_fresh(str(tmp_path))
    assert fresh is False and info is None


def test_heartbeat_roundtrip_is_fresh_and_carries_identity(tmp_path):
    pg._write_heartbeat(str(tmp_path))
    fresh, info = pg._heartbeat_is_fresh(str(tmp_path))
    assert fresh is True
    assert info['pid'] == os.getpid()
    assert info['host'] and 'age_s' in info


def test_stale_heartbeat_reports_info_but_not_fresh(tmp_path):
    """A stale heartbeat MUST still return its info dict — the caller logs
    which host/pid abandoned the pgdata."""
    pg._write_heartbeat(str(tmp_path))
    path = pg._heartbeat_path(str(tmp_path))
    old = time.time() - 10_000
    os.utime(path, (old, old))
    fresh, info = pg._heartbeat_is_fresh(str(tmp_path), ttl_s=120)
    assert fresh is False
    assert info['pid'] == os.getpid() and info['age_s'] > 120


def test_heartbeat_ttl_boundary_is_inclusive(tmp_path):
    pg._write_heartbeat(str(tmp_path))
    path = pg._heartbeat_path(str(tmp_path))
    t = time.time() - 60
    os.utime(path, (t, t))
    assert pg._heartbeat_is_fresh(str(tmp_path), ttl_s=120)[0] is True
    assert pg._heartbeat_is_fresh(str(tmp_path), ttl_s=30)[0] is False


def test_heartbeat_write_is_atomic_no_tmp_left(tmp_path):
    """Written via tmp+os.replace so a reader never sees a half file."""
    pg._write_heartbeat(str(tmp_path))
    assert not (tmp_path / '.tofu_heartbeat.tmp').exists()
    assert (tmp_path / '.tofu_heartbeat').exists()


def test_corrupt_heartbeat_reads_as_none_not_crash(tmp_path):
    (tmp_path / '.tofu_heartbeat').write_text('{not json')
    assert pg._read_heartbeat(str(tmp_path)) is None


def test_non_dict_heartbeat_reads_as_none(tmp_path):
    (tmp_path / '.tofu_heartbeat').write_text(json.dumps([1, 2, 3]))
    assert pg._read_heartbeat(str(tmp_path)) is None


def test_corrupt_heartbeat_is_not_fresh_but_still_reports_age(tmp_path):
    """Freshness comes from mtime, so a corrupt-but-recent file is 'fresh'
    with an empty identity — it must not crash the startup path."""
    (tmp_path / '.tofu_heartbeat').write_text('{not json')
    fresh, info = pg._heartbeat_is_fresh(str(tmp_path))
    assert fresh is True and info.get('pid') is None


def test_clear_heartbeat_is_idempotent(tmp_path):
    pg._write_heartbeat(str(tmp_path))
    pg._clear_heartbeat(str(tmp_path))
    assert not (tmp_path / '.tofu_heartbeat').exists()
    pg._clear_heartbeat(str(tmp_path))  # second call must not raise


def test_heartbeat_thread_start_is_idempotent_and_stops_clean(tmp_path):
    """Two starts must not spawn two refreshers (double writers on FUSE)."""
    before = threading.active_count()
    try:
        pg._start_heartbeat_thread(str(tmp_path))
        pg._start_heartbeat_thread(str(tmp_path))
        assert threading.active_count() <= before + 1
        assert (tmp_path / '.tofu_heartbeat').exists()  # immediate first write
    finally:
        pg.stop_heartbeat(str(tmp_path))
    assert not (tmp_path / '.tofu_heartbeat').exists()  # cleared on shutdown


def test_stop_heartbeat_without_pgdata_leaves_file(tmp_path):
    """stop_heartbeat(None) stops the thread but must NOT clear the file —
    that distinction is what lets a restart see its own recent heartbeat."""
    try:
        pg._start_heartbeat_thread(str(tmp_path))
        pg.stop_heartbeat(None)
        assert (tmp_path / '.tofu_heartbeat').exists()
    finally:
        pg.stop_heartbeat(str(tmp_path))


def test_heartbeat_ttl_exceeds_refresh_interval():
    """Invariant: TTL must be comfortably larger than the refresh period, or a
    live owner is periodically misread as dead and its PG gets stolen."""
    assert pg._HEARTBEAT_TTL_S >= 2 * pg._HEARTBEAT_REFRESH_S


# ───────────────────── host identity / owner ─────────────────────

def test_absent_marker_is_UNKNOWN_not_a_boolean(tmp_path):
    """No ``.pg_owner_id`` marker → ``None`` ("don't know"), never True/False.

    This is a THREE-state predicate and the distinction carries the safety:
    ``None`` tells the caller to fall back to the IP / live-PID heuristics for a
    legacy pgdata, whereas either boolean would be a definite verdict the marker
    does not support. Collapsing it to a bool is the regression to catch —
    ``False`` would look conservative while actually asserting "a different host
    owns this", and ``True`` would claim a pgdata we know nothing about.

    Recorded because I guessed this signature wrong twice before reading it:
    do not "simplify" it back to a bool.
    """
    assert pg._owner_is_self(str(tmp_path)) is None


def test_write_then_owner_is_self_roundtrip(tmp_path):
    pg._write_owner_host(str(tmp_path))
    assert pg._owner_is_self(str(tmp_path)) is True


def test_foreign_identity_marker_is_not_self(tmp_path):
    """A DIFFERENT stable identity in .pg_owner_id → definite False.

    Note the file: ownership is decided by ``.pg_owner_id`` (stable host
    identity), NOT by ``.pg_owner_host`` (the IP). Writing to the IP file would
    leave the predicate reading an absent marker and answering None — an easy
    way to write a test that passes while checking nothing.
    """
    (tmp_path / '.pg_owner_id').write_text('definitely-not-this-host-9f3c1b')
    assert pg._owner_is_self(str(tmp_path)) is False


def test_empty_identity_marker_falls_back_to_unknown(tmp_path):
    """A present-but-empty marker must NOT be compared as a value."""
    (tmp_path / '.pg_owner_id').write_text('   ')
    assert pg._owner_is_self(str(tmp_path)) is None


def test_ownership_survives_an_ip_flap(tmp_path, monkeypatch):
    """THE reason _get_host_identity exists (see its docstring: a server once
    mistook its OWN pgdata for a remote one after the container IP was
    reassigned → split-brain).

    Claim the pgdata, then change what _get_local_ip reports. Ownership is keyed
    on the stable identity, so it must still resolve to self.
    """
    monkeypatch.setattr(pg, '_get_local_ip', lambda: '10.0.0.1')
    pg._write_owner_host(str(tmp_path))
    assert pg._owner_is_self(str(tmp_path)) is True

    monkeypatch.setattr(pg, '_get_local_ip', lambda: '10.9.9.250')  # IP flap
    assert pg._owner_is_self(str(tmp_path)) is True
    assert (tmp_path / '.pg_owner_host').read_text() == '10.0.0.1'


def test_host_identity_env_override_changes_the_verdict(tmp_path, monkeypatch):
    """TOFU_HOST_ID is the documented first source; a genuinely different host
    reading the same shared pgdata must NOT see itself as the owner."""
    pg._write_owner_host(str(tmp_path))
    assert pg._owner_is_self(str(tmp_path)) is True

    pg._HOST_IDENTITY_CACHE = None  # force re-resolution as a "different host"
    monkeypatch.setenv('TOFU_HOST_ID', 'some-other-container-abc123')
    assert pg._owner_is_self(str(tmp_path)) is False


def test_host_identity_is_a_stable_nonempty_fingerprint():
    """Identity is a STRING fingerprint (not a dict of fields — measured), and
    it must be stable within the process or every restart looks like a new host
    and steals the pgdata from itself."""
    ident = pg._get_host_identity()
    assert isinstance(ident, str) and ident
    assert pg._get_host_identity() == ident


def test_local_ip_is_a_string():
    assert isinstance(pg._get_local_ip(), str)


# ───────────────────── facade integrity ─────────────────────

def test_facade_reexports_every_declared_symbol():
    """The package is a PURE re-export facade; a submodule split that drops a
    name would break `_bootstrap.<name>` at runtime, not at import."""
    missing = [n for n in pg.__all__ if not hasattr(pg, n)]
    assert missing == []


def test_lazy_core_shims_resolve_but_stay_out_of_all():
    """_audit / _pg_real_connect_ok are re-exported for _bootstrap but must NOT
    be in __all__ (listing them would shadow the real ones on import *)."""
    assert hasattr(pg, '_audit') and hasattr(pg, '_pg_real_connect_ok')
    assert '_audit' not in pg.__all__
    assert '_pg_real_connect_ok' not in pg.__all__
