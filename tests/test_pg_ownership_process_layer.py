"""pg_ownership process/lock layer: copy-detect, standalone heal, startup flock.

WHY THIS FILE EXISTS
────────────────────
This is the second half of the cross-host interlock (the first half — flock
enforcement probing, heartbeat freshness, host identity — is pinned by
`tests/test_pg_ownership.py`). What lives here decides, at startup, whether
THIS host may own the pgdata:

  * `_identity`   — was this directory COPIED here? (absolute-path stamp)
  * `_ownership`  — standalone mode: is an inherited remote marker stale?
  * `_lock`       — the cross-host startup flock that stops two hosts from
                    racing into `pg_ctl start` inside the same 60-120s window
  * `_binaries`   — pidfile liveness (the IP-independent ground truth) and the
                    `_pg_already_running_on_another_machine` decision that
                    stitches all of the above together

Every wrong answer here is a double-start on one pgdata → WAL / pg_subtrans
corruption, i.e. the whole database. Measured before this file existed:
`_binaries` 8%, `_identity` 15%, `_lock` 14%, `_ownership` 21%.

THE DESIGN RULE THESE TESTS ENFORCE: **the PID beats the IP.**
`.pg_owner_host` is derived from `_get_local_ip()`, which flaps when a
container's IP is reassigned. A host then mistakes its OWN postmaster for a
remote one, deletes the pidfile, and starts a second postmaster. So every
"this looks remote" path has an IP-independent override, and each override is
pinned SEPARATELY below — they are fail-safe guards, and a fail-safe guard that
is only tested in aggregate can be deleted one branch at a time while the suite
stays green.

Reusing the precedents from the sibling file (per the epic):
  * `_reset_process_caches` — the facade holds PROCESS-lifetime state
    (`_startup_lock_fd`, `_PG_STARTED_BY_US`, `_HOST_IDENTITY_CACHE`,
    `_flock_enforced`); without a reset the first test's verdict silently
    decides every later one.
  * simulate the FILESYSTEM, not the function under test (see
    `test_probe_detects_a_SILENT_NOOP_filesystem` there, and the fake-`fcntl`
    contention test here).
"""

import errno
import json
import os
import sys

import pytest

import lib.database._pg_ownership as pg

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_process_caches():
    """Restore every facade-canonical process global around each test.

    All four are plain scalars ON THE FACADE (not on the submodules) — the
    submodules deliberately read/write them through `_pkg.X` so a test can
    steer them. Reset list mirrors the module docstrings' "owns the X global"
    statements.
    """
    saved = {
        'fd': pg._startup_lock_fd,
        'owned': pg._PG_STARTED_BY_US,
        'hostid': pg._HOST_IDENTITY_CACHE,
        'flock': pg._flock_enforced,
    }
    pg._startup_lock_fd = None
    pg._PG_STARTED_BY_US = False
    yield
    # Close a fd a test left behind before restoring, or it leaks for the run.
    leaked = pg._startup_lock_fd
    if leaked is not None and leaked != saved['fd']:
        try:
            os.close(leaked)
        except OSError:
            pass
    pg._startup_lock_fd = saved['fd']
    pg._PG_STARTED_BY_US = saved['owned']
    pg._HOST_IDENTITY_CACHE = saved['hostid']
    pg._flock_enforced = saved['flock']


# ═════════════════════════════════════════════════════════════════
#  _identity — the copy/move detector
# ═════════════════════════════════════════════════════════════════

def test_absent_stamp_is_not_a_copy(tmp_path):
    """A legacy pgdata (predating the stamp) or a fresh initdb must NOT be
    mistaken for a copy — that would clear a live host's ownership markers."""
    assert pg._pgdata_was_copied(str(tmp_path)) == (False, None)


def test_stamp_roundtrip_records_the_canonical_path(tmp_path):
    pg._write_instance_stamp(str(tmp_path))
    stamp = pg._read_instance_stamp(str(tmp_path))
    assert stamp['path'] == os.path.realpath(str(tmp_path))
    assert stamp['id'] and stamp['created']


def test_restamping_the_same_path_keeps_the_id_stable(tmp_path):
    """Idempotent for the path: a restart must not mint a new identity, or
    every boot would look like a fresh instance."""
    pg._write_instance_stamp(str(tmp_path))
    first = pg._read_instance_stamp(str(tmp_path))
    pg._write_instance_stamp(str(tmp_path))
    second = pg._read_instance_stamp(str(tmp_path))
    assert second['id'] == first['id']
    assert second['created'] == first['created']
    assert second.get('restamped') is None, 'same-path write must be a no-op'


def test_moved_pgdata_is_detected_as_copied(tmp_path):
    """THE detector: the stamp records where the directory WAS; a different
    current path means it was copied/moved and its markers are inherited."""
    origin = tmp_path / 'origin'
    origin.mkdir()
    pg._write_instance_stamp(str(origin))

    copy = tmp_path / 'copy'
    copy.mkdir()
    (copy / '.pg_instance_id').write_text(
        (origin / '.pg_instance_id').read_text(), encoding='utf-8')

    was_copied, stamped = pg._pgdata_was_copied(str(copy))
    assert was_copied is True
    assert stamped == os.path.realpath(str(origin))


def test_same_path_via_a_symlink_is_not_a_copy(tmp_path):
    """Legitimate cross-host sharing addresses the SAME physical directory,
    possibly through a symlink. realpath canonicalisation must absorb that —
    otherwise every symlinked deployment self-heals on every boot."""
    real = tmp_path / 'real'
    real.mkdir()
    pg._write_instance_stamp(str(real))
    link = tmp_path / 'link'
    link.symlink_to(real)
    assert pg._pgdata_was_copied(str(link))[0] is False


def test_trailing_slash_and_dotdot_do_not_look_like_a_copy(tmp_path):
    real = tmp_path / 'pgdata'
    real.mkdir()
    pg._write_instance_stamp(str(real))
    assert pg._pgdata_was_copied(str(real) + '/')[0] is False
    assert pg._pgdata_was_copied(str(tmp_path / 'x' / '..' / 'pgdata'))[0] is False


def test_malformed_stamp_reads_as_absent_not_as_a_copy(tmp_path):
    """Fail SAFE: an unreadable stamp must not trigger a self-heal that clears
    a live peer's markers."""
    (tmp_path / '.pg_instance_id').write_text('{not json')
    assert pg._read_instance_stamp(str(tmp_path)) is None
    assert pg._pgdata_was_copied(str(tmp_path)) == (False, None)


def test_stamp_without_a_path_key_is_rejected(tmp_path):
    (tmp_path / '.pg_instance_id').write_text(json.dumps({'id': 'x'}))
    assert pg._read_instance_stamp(str(tmp_path)) is None


def test_stamp_write_is_atomic_no_tmp_left(tmp_path):
    pg._write_instance_stamp(str(tmp_path))
    assert not (tmp_path / '.pg_instance_id.tmp').exists()


# ── _clear_ownership_markers: what it removes and what it must NOT ──

def _seed_markers(d, *, with_pidfile=True, with_data=True):
    """Plant a full set of ownership markers (+ data files) in a pgdata.

    Callable MORE THAN ONCE on the same directory — a test that clears markers
    and re-seeds to check the other flag combination needs that, and
    `mkdir()` without exist_ok raised FileExistsError on the second call.
    """
    (d / '.pg_owner_host').write_text('10.0.0.9')
    (d / '.pg_owner_id').write_text('some-host-id')
    (d / '.tofu_heartbeat').write_text('{}')
    if with_pidfile:
        (d / 'postmaster.pid').write_text('4242\n/pgdata\n')
        (d / 'postmaster.opts').write_text('opts')
    if with_data:
        (d / 'PG_VERSION').write_text('16')
        (d / 'postgresql.conf').write_text('# conf')
        (d / 'base').mkdir(exist_ok=True)


def test_clear_markers_removes_ownership_but_never_data(tmp_path):
    """DATA loss here is unrecoverable, so the negative half of this assertion
    matters more than the positive half."""
    _seed_markers(tmp_path)
    removed = pg._clear_ownership_markers(str(tmp_path))
    assert '.pg_owner_host' in removed and '.tofu_heartbeat' in removed
    assert (tmp_path / 'PG_VERSION').exists()
    assert (tmp_path / 'postgresql.conf').exists()
    assert (tmp_path / 'base').is_dir()


def test_clear_markers_removes_pidfile_only_when_asked(tmp_path):
    """The copy self-heal passes remove_pidfile=False: deleting a pidfile whose
    PID is a LIVE postgres is how a second postmaster gets started."""
    _seed_markers(tmp_path)
    pg._clear_ownership_markers(str(tmp_path), remove_pidfile=False)
    assert (tmp_path / 'postmaster.pid').exists()

    _seed_markers(tmp_path)
    pg._clear_ownership_markers(str(tmp_path), remove_pidfile=True)
    assert not (tmp_path / 'postmaster.pid').exists()
    assert not (tmp_path / 'postmaster.opts').exists()


def test_clear_markers_is_idempotent_and_reports_only_real_removals(tmp_path):
    _seed_markers(tmp_path)
    first = pg._clear_ownership_markers(str(tmp_path))
    assert first
    assert pg._clear_ownership_markers(str(tmp_path)) == []


# ── _heal_if_copied: the end-to-end self-heal ──

def test_heal_if_copied_clears_inherited_markers_and_keeps_pidfile(tmp_path):
    origin = tmp_path / 'origin'
    origin.mkdir()
    pg._write_instance_stamp(str(origin))
    copy = tmp_path / 'copy'
    copy.mkdir()
    (copy / '.pg_instance_id').write_text(
        (origin / '.pg_instance_id').read_text(), encoding='utf-8')
    _seed_markers(copy)

    assert pg._heal_if_copied(str(copy)) is True
    assert not (copy / '.pg_owner_host').exists()
    assert not (copy / '.tofu_heartbeat').exists()
    assert (copy / 'postmaster.pid').exists(), (
        'the pidfile must survive — a live postgres may still own it')
    assert (copy / 'PG_VERSION').exists()


def test_heal_if_copied_is_a_noop_for_a_same_path_pgdata(tmp_path):
    """Same-path multi-host sharing is LEGITIMATE failover; healing there would
    steal a live peer's PG."""
    pg._write_instance_stamp(str(tmp_path))
    _seed_markers(tmp_path)
    assert pg._heal_if_copied(str(tmp_path)) is False
    assert (tmp_path / '.pg_owner_host').exists()


# ═════════════════════════════════════════════════════════════════
#  _ownership — standalone-mode heal + the process-ownership flag
# ═════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('raw', ['1', 'true', 'yes', 'on', 'TRUE', ' Yes '])
def test_standalone_mode_accepts_documented_truthy(monkeypatch, raw):
    monkeypatch.setenv('TOFU_PG_STANDALONE', raw)
    assert pg._standalone_mode() is True


@pytest.mark.parametrize('raw', ['', '0', 'false', 'no', 'maybe'])
def test_standalone_mode_rejects_everything_else(monkeypatch, raw):
    monkeypatch.setenv('TOFU_PG_STANDALONE', raw)
    assert pg._standalone_mode() is False


def test_standalone_mode_defaults_off(monkeypatch):
    """Default OFF keeps same-path multi-host failover working — turning it on
    by default would disable the heartbeat handoff for those deployments."""
    monkeypatch.delenv('TOFU_PG_STANDALONE', raising=False)
    assert pg._standalone_mode() is False


@pytest.fixture
def standalone(monkeypatch):
    """Standalone ON + a remote owner marker + all three guards开 (i.e. not
    triggered), so each test can flip exactly one guard."""
    monkeypatch.setenv('TOFU_PG_STANDALONE', '1')
    monkeypatch.setattr(pg, '_owner_is_self', lambda p: None)
    monkeypatch.setattr(pg, '_read_pg_host_from_pidfile', lambda p: '10.9.9.9')
    monkeypatch.setattr(pg, '_get_local_ip', lambda: '10.0.0.1')
    monkeypatch.setattr(pg, '_pidfile_pid_is_live_local_postgres', lambda p: False)
    cleared = []
    monkeypatch.setattr(pg, '_clear_ownership_markers',
                        lambda p, **kw: cleared.append(kw) or [])
    monkeypatch.setattr(pg, '_clear_heartbeat', lambda p: None)
    return cleared


def test_standalone_heals_an_inherited_remote_marker(standalone, tmp_path):
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is True
    assert standalone and standalone[0]['remove_pidfile'] is False, (
        'the pidfile must not be removed — a live local postgres may own it')


def test_standalone_heal_is_off_unless_the_flag_is_set(standalone, monkeypatch, tmp_path):
    monkeypatch.delenv('TOFU_PG_STANDALONE', raising=False)
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is False
    assert standalone == []


def test_stable_identity_beats_an_ip_flap(standalone, monkeypatch, tmp_path):
    """GUARD 1 — `.pg_owner_id` says the pgdata is OURS, so owner_host != local_ip
    is an IP flap, not an inherited remote marker. Clearing here would discard
    our own live ownership."""
    monkeypatch.setattr(pg, '_owner_is_self', lambda p: True)
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is False
    assert standalone == []


def test_no_owner_marker_means_nothing_to_heal(standalone, monkeypatch, tmp_path):
    monkeypatch.setattr(pg, '_read_pg_host_from_pidfile', lambda p: None)
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is False
    assert standalone == []


@pytest.mark.parametrize('owner', ['10.0.0.1', 'localhost', '127.0.0.1'])
def test_local_owner_marker_is_not_inherited(standalone, monkeypatch, tmp_path, owner):
    """GUARD 2 — the marker already names THIS host under any of its spellings."""
    monkeypatch.setattr(pg, '_read_pg_host_from_pidfile', lambda p: owner)
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is False
    assert standalone == []


def test_live_local_postmaster_blocks_the_standalone_heal(standalone, monkeypatch, tmp_path):
    """GUARD 3 — the PID beats the IP. A live local postgres means we already
    own the pgdata even if `.pg_owner_host` records a stale foreign IP."""
    monkeypatch.setattr(pg, '_pidfile_pid_is_live_local_postgres', lambda p: True)
    assert pg._heal_if_standalone_remote_owner(str(tmp_path)) is False
    assert standalone == []


def test_ownership_flag_starts_false_and_is_set_by_marking(monkeypatch, tmp_path):
    assert pg.is_pg_owned_locally() is False
    monkeypatch.setattr(pg, '_start_heartbeat_thread', lambda p: None)
    pg._mark_pg_owned_locally(str(tmp_path))
    assert pg.is_pg_owned_locally() is True


def test_marking_with_pgdata_stamps_identity_and_starts_heartbeat(monkeypatch, tmp_path):
    """The stamp is what makes a LATER copy detectable; skipping it silently
    disables copy-detect for this pgdata forever."""
    beats = []
    monkeypatch.setattr(pg, '_start_heartbeat_thread', lambda p: beats.append(p))
    pg._mark_pg_owned_locally(str(tmp_path))
    assert (tmp_path / '.pg_instance_id').exists()
    assert beats == [str(tmp_path)]


def test_marking_without_pgdata_only_flips_the_flag(monkeypatch):
    monkeypatch.setattr(pg, '_start_heartbeat_thread',
                        lambda p: pytest.fail('must not start a heartbeat'))
    pg._mark_pg_owned_locally()
    assert pg.is_pg_owned_locally() is True


# ═════════════════════════════════════════════════════════════════
#  _lock — the cross-host startup flock
# ═════════════════════════════════════════════════════════════════

@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_startup_lock_is_acquired_and_released(tmp_path):
    assert pg._try_acquire_startup_lock(str(tmp_path)) is True
    assert pg._startup_lock_fd is not None
    assert (tmp_path / '.tofu_pg_start.lock').exists()
    pg._release_startup_lock()
    assert pg._startup_lock_fd is None


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX advisory locks only')
def test_reacquiring_in_the_same_process_is_idempotent(tmp_path):
    """Re-entrant acquisition must return True WITHOUT opening a second fd —
    otherwise a retry path leaks one fd per attempt."""
    assert pg._try_acquire_startup_lock(str(tmp_path)) is True
    first_fd = pg._startup_lock_fd
    assert pg._try_acquire_startup_lock(str(tmp_path)) is True
    assert pg._startup_lock_fd == first_fd


def test_release_without_holding_is_safe(tmp_path):
    pg._release_startup_lock()
    pg._release_startup_lock()
    assert pg._startup_lock_fd is None


def test_contended_lock_refuses_so_pg_ctl_is_skipped(tmp_path, monkeypatch):
    """THE anti-race decision: when another host holds the lock we must return
    False so the caller does NOT run `pg_ctl start`.

    Simulates the FILESYSTEM (a fake `fcntl` raising EWOULDBLOCK), not the
    function under test — the same technique the sibling suite uses for the
    no-op-FS probe. A real second holder would need a second process; the
    errno IS the contract the code branches on.
    """
    import fcntl as real_fcntl

    class _ContendedFcntl:
        LOCK_EX = real_fcntl.LOCK_EX
        LOCK_NB = real_fcntl.LOCK_NB
        LOCK_UN = real_fcntl.LOCK_UN

        @staticmethod
        def flock(_fd, op):
            if op & real_fcntl.LOCK_UN:
                return None
            raise OSError(errno.EWOULDBLOCK, 'Resource temporarily unavailable')

    monkeypatch.setitem(sys.modules, 'fcntl', _ContendedFcntl)
    assert pg._try_acquire_startup_lock(str(tmp_path)) is False, (
        'a held lock must block our pg_ctl start — returning True here is the '
        'concurrent-start that truncates WAL')
    assert pg._startup_lock_fd is None, 'a refused lock must not retain an fd'


def test_unsupported_flock_degrades_to_noop_and_proceeds(tmp_path, monkeypatch):
    """A filesystem without advisory locks must NOT block startup — the
    pre-fix behaviour is preserved with a loud warning (single-host deploys on
    such mounts have to keep working)."""
    import fcntl as real_fcntl

    class _NoLockFcntl:
        LOCK_EX = real_fcntl.LOCK_EX
        LOCK_NB = real_fcntl.LOCK_NB
        LOCK_UN = real_fcntl.LOCK_UN

        @staticmethod
        def flock(_fd, op):
            if op & real_fcntl.LOCK_UN:
                return None
            raise OSError(errno.ENOLCK, 'No locks available')

    monkeypatch.setitem(sys.modules, 'fcntl', _NoLockFcntl)
    assert pg._try_acquire_startup_lock(str(tmp_path)) is True
    assert pg._startup_lock_fd is not None, (
        'the degraded path still retains the fd so the file is held open')


def test_unopenable_lock_file_degrades_to_proceeding(tmp_path, monkeypatch):
    """Failing to even open the lock file must degrade to "let the caller try",
    not to a hard refusal that would leave PG permanently unstartable."""
    def boom(*a, **k):
        raise OSError(errno.EACCES, 'permission denied')

    monkeypatch.setattr(os, 'open', boom)
    assert pg._try_acquire_startup_lock(str(tmp_path)) is True


def test_lock_lives_inside_pgdata_so_every_racing_host_sees_it(tmp_path):
    """The lock is only a cross-HOST guard if it sits on the shared mount."""
    assert pg._startup_lock_path(str(tmp_path)) == str(tmp_path / '.tofu_pg_start.lock')


# ═════════════════════════════════════════════════════════════════
#  _binaries — pidfile liveness + the remote-owner decision
# ═════════════════════════════════════════════════════════════════

def test_pidfile_liveness_false_when_absent_or_unparseable(tmp_path):
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is False
    (tmp_path / 'postmaster.pid').write_text('not-a-pid\n')
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is False


def test_pidfile_liveness_false_for_a_dead_pid(tmp_path, monkeypatch):
    (tmp_path / 'postmaster.pid').write_text('999999\n')
    import lib.compat as compat
    monkeypatch.setattr(compat, 'is_process_alive', lambda pid: False)
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is False


def test_pidfile_liveness_false_for_a_live_non_postgres_pid(tmp_path, monkeypatch):
    """A genuinely stale pidfile whose PID got recycled by another program."""
    (tmp_path / 'postmaster.pid').write_text(f'{os.getpid()}\n')
    import lib.compat as compat
    monkeypatch.setattr(compat, 'is_process_alive', lambda pid: True)
    monkeypatch.setattr(compat, 'is_process_named', lambda pid, name: False)
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is False


def test_pidfile_liveness_true_for_a_live_postgres(tmp_path, monkeypatch):
    (tmp_path / 'postmaster.pid').write_text(f'{os.getpid()}\n')
    import lib.compat as compat
    monkeypatch.setattr(compat, 'is_process_alive', lambda pid: True)
    monkeypatch.setattr(compat, 'is_process_named', lambda pid, name: True)
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is True


def test_unintrospectable_live_pid_assumes_postgres(tmp_path, monkeypatch):
    """FAIL SAFE, and note the direction: when the name check itself fails (no
    /proc perms), assume it IS our postgres. Guessing "not postgres" would
    permit a double-start, which is far worse than refusing to start."""
    (tmp_path / 'postmaster.pid').write_text(f'{os.getpid()}\n')
    import lib.compat as compat
    monkeypatch.setattr(compat, 'is_process_alive', lambda pid: True)
    monkeypatch.setattr(compat, 'is_process_named',
                        lambda pid, name: (_ for _ in ()).throw(OSError('no perms')))
    assert pg._pidfile_pid_is_live_local_postgres(str(tmp_path)) is True


def test_owner_host_marker_read_and_missing(tmp_path):
    assert pg._read_pg_host_from_pidfile(str(tmp_path)) is None
    (tmp_path / '.pg_owner_host').write_text('  10.2.3.4  \n')
    assert pg._read_pg_host_from_pidfile(str(tmp_path)) == '10.2.3.4'
    (tmp_path / '.pg_owner_host').write_text('   ')
    assert pg._read_pg_host_from_pidfile(str(tmp_path)) is None


# ── the composed decision ──

@pytest.fixture
def remote_env(monkeypatch):
    """Neutral baseline for `_pg_already_running_on_another_machine`:
    no heal fires, identity is unknown, no live local postgres."""
    monkeypatch.setattr(pg, '_heal_if_copied', lambda p: False)
    monkeypatch.setattr(pg, '_heal_if_standalone_remote_owner', lambda p: False)
    monkeypatch.setattr(pg, '_owner_is_self', lambda p: None)
    monkeypatch.setattr(pg, '_get_local_ip', lambda: '10.0.0.1')
    monkeypatch.setattr(pg, '_get_host_identity', lambda: 'this-host')
    monkeypatch.setattr(pg, '_pidfile_pid_is_live_local_postgres', lambda p: False)
    monkeypatch.setattr(pg, '_pg_real_connect_ok',
                        lambda *a, **k: True)
    return monkeypatch


def _pidfile(d, pid=4242):
    (d / 'postmaster.pid').write_text(f'{pid}\n{d}\n1700000000\n')


def test_copy_heal_short_circuits_the_whole_decision(tmp_path, remote_env):
    """A copied pgdata must report NO remote owner — every inherited marker
    belongs to the original instance. This is the "silently connect to the
    source machine's PG" trap."""
    remote_env.setattr(pg, '_heal_if_copied', lambda p: True)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(str(tmp_path), 15432) == (False, None)


def test_standalone_heal_short_circuits_the_decision(tmp_path, remote_env):
    remote_env.setattr(pg, '_heal_if_standalone_remote_owner', lambda p: True)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(str(tmp_path), 15432) == (False, None)


def test_absent_pidfile_means_pg_is_not_running(tmp_path, remote_env):
    assert pg._pg_already_running_on_another_machine(str(tmp_path), 15432) == (False, None)


def test_truncated_pidfile_is_treated_as_absent(tmp_path, remote_env):
    """postgres writes many lines; a 1-line file is mid-write or corrupt."""
    (tmp_path / 'postmaster.pid').write_text('4242\n')
    assert pg._pg_already_running_on_another_machine(str(tmp_path), 15432) == (False, None)


def test_stable_identity_match_wins_over_a_foreign_owner_ip(tmp_path, remote_env):
    """`.pg_owner_id` == ours → the pgdata is OURS however the IP flapped.
    This runs BEFORE the IP comparison on purpose."""
    remote_env.setattr(pg, '_owner_is_self', lambda p: True)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(str(tmp_path), 15432) == (False, None)


def test_remote_owner_is_reported_with_its_host(tmp_path, remote_env):
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(
        str(tmp_path), 15432) == (True, '10.9.9.9')


def test_live_local_postmaster_overrides_a_foreign_owner_ip(tmp_path, remote_env):
    """THE IP-flap fix, at the composed level: the pidfile PID is a live local
    postgres, so we own the pgdata even though `.pg_owner_host` looks foreign.
    Reporting "remote" here previously led to deleting the pidfile and starting
    a SECOND postmaster → pg_subtrans corruption."""
    remote_env.setattr(pg, '_pidfile_pid_is_live_local_postgres', lambda p: True)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(
        str(tmp_path), 15432) == (False, None)


def test_foreign_identity_marks_remote_even_when_ips_coincide(tmp_path, remote_env):
    """`_owner_is_self is False` is authoritative proof of remoteness — two
    containers can transiently report the SAME flapping IP."""
    remote_env.setattr(pg, '_owner_is_self', lambda p: False)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.0.0.1')   # == our local ip
    running, host = pg._pg_already_running_on_another_machine(str(tmp_path), 15432)
    assert running is True


def test_unreachable_remote_owner_is_still_reported_as_remote(tmp_path, remote_env):
    """Deferring to an unreachable owner is the CORRECT conservative answer:
    two postmasters on one pgdata is worse than a failed connection."""
    remote_env.setattr(pg, '_pg_real_connect_ok', lambda *a, **k: False)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.9.9.9')
    assert pg._pg_already_running_on_another_machine(
        str(tmp_path), 15432) == (True, '10.9.9.9')


def test_local_owner_with_live_postgres_pid_is_not_remote(tmp_path, remote_env):
    remote_env.setattr(pg, '_read_pg_host_from_pidfile', lambda p: '10.0.0.1')
    import lib.compat as compat
    remote_env.setattr(compat, 'is_process_alive', lambda pid: True)
    remote_env.setattr(compat, 'is_process_named', lambda pid, name: True)
    _pidfile(tmp_path)
    (tmp_path / '.pg_owner_host').write_text('10.0.0.1')
    assert pg._pg_already_running_on_another_machine(
        str(tmp_path), 15432) == (False, None)


# ── port + conf helpers ──

def test_find_free_port_returns_a_port_in_range():
    port = pg._find_free_port(15432, 15440)
    assert 15432 <= port < 15440


def test_find_free_port_skips_a_listening_port():
    """A bound port must not be handed out — PG would fail to start on it."""
    import socket
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(1)
    taken = s.getsockname()[1]
    try:
        assert pg._find_free_port(taken, taken + 3) != taken
    finally:
        s.close()


def test_find_free_port_falls_back_to_start_when_range_exhausted(monkeypatch):
    """Returning `start` (not None / not raising) keeps the caller's contract:
    it always gets an int and reports the real failure later."""
    import socket

    class _AllBusy:
        def settimeout(self, _):
            pass

        def connect_ex(self, _addr):
            return 0        # 0 == connected == port busy

        def close(self):
            pass

    monkeypatch.setattr(socket, 'socket', lambda *a, **k: _AllBusy())
    assert pg._find_free_port(15432, 15435) == 15432


def test_find_pg_binary_returns_bare_name_when_missing():
    """Returning the bare name lets subprocess raise FileNotFoundError with a
    readable message instead of this helper inventing a fake path."""
    assert pg._find_pg_binary('definitely-not-a-real-pg-binary-xyz') == \
        'definitely-not-a-real-pg-binary-xyz'


def test_find_pg_binary_finds_something_on_path():
    found = pg._find_pg_binary('sh')
    assert os.path.isabs(found) and os.path.exists(found)


def test_get_username_never_raises():
    assert isinstance(pg._get_username(), str) and pg._get_username()


def test_fix_unix_socket_conf_is_a_noop_without_a_conf(tmp_path):
    pg._fix_unix_socket_conf(str(tmp_path))   # must not raise
    assert not (tmp_path / 'postgresql.conf').exists(), (
        'a missing conf must stay missing — creating one would shadow PG defaults')


def test_fix_unix_socket_conf_disables_sockets_on_a_fuse_path(tmp_path, monkeypatch):
    """FUSE cannot host AF_UNIX sockets, so PG must be told not to try.

    The trigger is `IS_LINUX and pgdata.startswith('/mnt/')`. Rather than
    fabricate a real /mnt directory (not writable here), patch the module's
    IS_LINUX and hand it a path that satisfies the prefix by construction:
    pytest's tmp_path lives under /tmp, so we redirect the conf lookup by
    patching `os.path.join` for this one call... which would be fragile.
    Instead: patch the module-level prefix check inputs directly by giving the
    helper a directory we CREATE under a '/mnt'-shaped subtree inside tmp_path
    and patching startswith's subject via a tiny shim class.

    ★ An earlier version of this test used a symlink plus two no-op
    monkeypatches and asserted `'/mnt/x'.startswith('/mnt/')` — i.e. it
    verified Python's str method, not the production branch, and would stay
    green with the whole function deleted. Rewritten to assert the FILE
    CONTENT actually changes.
    """
    import lib.database._pg_ownership._binaries as binmod
    monkeypatch.setattr(binmod, 'IS_WINDOWS', False)
    monkeypatch.setattr(binmod, 'IS_LINUX', True)

    pgdata = tmp_path / 'pgdata'
    pgdata.mkdir()
    conf = pgdata / 'postgresql.conf'
    conf.write_text("port = 5432\nunix_socket_directories = '/tmp'\n")

    # The helper branches on the pgdata STRING prefix; feed it a str subclass
    # that reports the /mnt/ prefix while still resolving to our real dir.
    class _MntPath(str):
        def startswith(self, prefix, *a):          # noqa: A003
            if prefix == '/mnt/':
                return True
            return str.startswith(self, prefix, *a)

    pg._fix_unix_socket_conf(_MntPath(str(pgdata)))
    assert "unix_socket_directories = ''" in conf.read_text(), (
        'the FUSE branch did not disable unix sockets — PG would try to create '
        'an AF_UNIX socket the filesystem cannot host')


def test_fix_unix_socket_conf_is_idempotent(tmp_path, monkeypatch):
    """Already-disabled conf must be left byte-identical (no needless rewrite
    of a file PG may be reading)."""
    import lib.database._pg_ownership._binaries as binmod
    monkeypatch.setattr(binmod, 'IS_WINDOWS', False)
    monkeypatch.setattr(binmod, 'IS_LINUX', True)

    pgdata = tmp_path / 'pgdata'
    pgdata.mkdir()
    conf = pgdata / 'postgresql.conf'
    original = "port = 5432\nunix_socket_directories = ''\n"
    conf.write_text(original)

    class _MntPath(str):
        def startswith(self, prefix, *a):          # noqa: A003
            return True if prefix == '/mnt/' else str.startswith(self, prefix, *a)

    pg._fix_unix_socket_conf(_MntPath(str(pgdata)))
    assert conf.read_text() == original


def test_fix_unix_socket_conf_leaves_local_disk_alone(tmp_path, monkeypatch):
    """On a local-disk Linux/macOS pgdata, Unix sockets work and are FASTER —
    patching them off there would be a gratuitous regression."""
    import lib.database._pg_ownership._binaries as binmod
    monkeypatch.setattr(binmod, 'IS_WINDOWS', False)
    monkeypatch.setattr(binmod, 'IS_LINUX', True)

    pgdata = tmp_path / 'pgdata'
    pgdata.mkdir()
    conf = pgdata / 'postgresql.conf'
    original = "port = 5432\nunix_socket_directories = '/tmp'\n"
    conf.write_text(original)

    pg._fix_unix_socket_conf(str(pgdata))   # tmp_path is NOT under /mnt/
    assert conf.read_text() == original
