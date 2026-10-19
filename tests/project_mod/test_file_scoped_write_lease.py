"""File admission cooperates with whole-checkout protection, not opaque writers."""
from contextlib import contextmanager
import hashlib
import os
import subprocess
import sys
import threading

import pytest

from lib.project_mod import workspace_lease as leases
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(_ops, '_salvage_root', lambda: str(tmp_path / 'salvage'))
    root = tmp_path / 'repo'
    root.mkdir()
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    (root / 'a.txt').write_text('a\n')
    (root / 'b.txt').write_text('b\n')
    return root


def _file_lease(path, **kwargs):
    return leases.workspace_file_write_lease(path, **kwargs)


def _clean():
    assert leases.workspace_lease_fds() == ()
    assert not leases._registry
    assert not leases._active_descriptors
    from lib.project_mod import post_commit
    assert not vars(post_commit._deferring)


def _closed(fds):
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def _thread(call):
    errors = []

    def run():
        try:
            call()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    return worker, errors


def _join(worker, errors):
    worker.join(5)
    assert not worker.is_alive(), 'lease worker failed to quiesce'
    assert not errors


@contextmanager
def _process_holder(path, api='workspace_file_write_lease'):
    code = (
        'import sys; from lib.project_mod import workspace_lease as m; '
        'cm=getattr(m,sys.argv[2])(sys.argv[1]); cm.__enter__(); '
        'print("ready",flush=True); sys.stdin.read()')
    child = subprocess.Popen([sys.executable, '-c', code, str(path), api],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        yield child
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()


def test_different_files_overlap_but_same_file_and_whole_checkout_do_not(repo):
    with _file_lease(repo / 'a.txt'):
        first = leases.workspace_lease_fds()
        assert len(first) >= 2
        with _file_lease(repo / 'a.txt'):
            assert leases.workspace_lease_fds() == first

        def contender():
            with _file_lease(repo / 'b.txt'):
                other = leases.workspace_lease_fds()
                assert len(other) >= 2
                assert set(first) & set(other)  # shared checkout and lifecycle barriers
                assert set(first) - set(other)  # distinct exclusive file descriptor
                with pytest.raises(leases.WorkspaceBusyError):
                    with _file_lease(repo / 'a.txt'):
                        pytest.fail('same target overlapped')
            for api in (leases.workspace_read_lease, leases.workspace_write_lease):
                with pytest.raises(leases.WorkspaceBusyError, match='Workspace write is active'):
                    with api(repo):
                        pytest.fail('whole checkout ignored file writer')

        worker, errors = _thread(contender)
        _join(worker, errors)
        assert leases.workspace_lease_fds() == first
    _closed(first)
    _clean()


def test_cross_process_different_files_and_canonical_aliases(repo, tmp_path):
    alias = tmp_path / 'alias'
    alias.symlink_to(repo, target_is_directory=True)
    symlink = repo / 'link.txt'
    symlink.symlink_to(repo / 'a.txt')
    with _process_holder(repo / 'a.txt'):
        with _file_lease(repo / 'b.txt'):
            assert len(leases.workspace_lease_fds()) >= 2
        for target in (repo / 'a.txt', alias / 'a.txt', symlink):
            with pytest.raises(leases.WorkspaceBusyError, match='holder type unknown'):
                with _file_lease(target):
                    pytest.fail('canonical target was not exclusive')
        for api in (leases.workspace_read_lease, leases.workspace_write_lease):
            with pytest.raises(leases.WorkspaceBusyError):
                with api(repo):
                    pytest.fail('whole checkout ignored another process file writer')
    _clean()


@pytest.mark.parametrize('api', ['workspace_read_lease', 'workspace_write_lease'])
def test_whole_checkout_process_blocks_all_files(repo, api):
    with _process_holder(repo, api):
        for name in ('a.txt', 'b.txt', 'new.txt'):
            with pytest.raises(leases.WorkspaceBusyError):
                with _file_lease(repo / name):
                    pytest.fail('file lease bypassed stable whole-checkout holder')
    _clean()


def test_whole_writer_reentry_retains_strong_lock(repo):
    with leases.workspace_write_lease(repo):
        fds = leases.workspace_lease_fds()
        with _file_lease(repo / 'a.txt'):
            assert set(fds).issubset(leases.workspace_lease_fds())
            with leases.workspace_read_lease(repo):
                with _file_lease(repo / 'b.txt'):
                    assert set(fds).issubset(leases.workspace_lease_fds())
        code = (
            'import sys; from lib.project_mod.workspace_lease import '
            'WorkspaceBusyError,workspace_file_write_lease; '
            '\ntry:\n with workspace_file_write_lease(sys.argv[1]): '
            'raise AssertionError("whole writer weakened")'
            '\nexcept WorkspaceBusyError: print("busy")')
        result = subprocess.run([sys.executable, '-c', code, str(repo / 'b.txt')],
                                capture_output=True, text=True, timeout=5, check=True)
        assert result.stdout.strip() == 'busy'
    _closed(fds)
    _clean()


def test_explicit_stable_read_and_file_writer_cannot_mix_on_one_thread(repo):
    with leases.workspace_read_lease(repo):
        fds = leases.workspace_lease_fds()
        with pytest.raises(leases.WorkspaceBusyError, match='Workspace read is active'):
            with _file_lease(repo / 'a.txt'):
                pytest.fail('file writer silently weakened explicit stable read')
        assert leases.workspace_lease_fds() == fds
    with _file_lease(repo / 'a.txt'):
        fds = leases.workspace_lease_fds()
        for api in (leases.workspace_read_lease, leases.workspace_write_lease):
            with pytest.raises(leases.WorkspaceBusyError):
                with api(repo):
                    pytest.fail('file barrier silently upgraded to a whole lock')
        assert leases.workspace_lease_fds() == fds
    _clean()


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_unknown_or_directory_target_falls_back_to_whole_checkout(repo, kind):
    target = repo / kind
    if kind == 'directory':
        target.mkdir()
    else:
        os.mkfifo(target)
    with _file_lease(target):
        assert leases.workspace_lease_fds()

        def contender():
            with pytest.raises(leases.WorkspaceBusyError):
                with _file_lease(repo / 'a.txt'):
                    pytest.fail('ambiguous target did not retain whole lock')

        worker, errors = _thread(contender)
        _join(worker, errors)
    _clean()


def test_new_targets_and_non_git_file_transactions(repo, tmp_path):
    with _file_lease(repo / 'new' / 'a.txt'):
        assert len(leases.workspace_lease_fds()) >= 2
        with _file_lease(repo / 'new' / 'b.txt'):
            assert len(leases.workspace_lease_fds()) >= 3
    with _file_lease(tmp_path / 'outside.txt'):
        assert leases.workspace_lease_fds()
        with _file_lease(tmp_path / 'different.txt'):
            assert leases.workspace_lease_fds()
    _clean()


@pytest.mark.parametrize('timeout', [-1, float('inf'), float('nan')])
def test_invalid_timeout_rejected_before_admission(repo, timeout):
    with pytest.raises(ValueError, match='nonnegative and finite'):
        with _file_lease(repo / 'a.txt', timeout=timeout):
            pytest.fail('invalid timeout entered')
    _clean()


def test_cancelled_non_git_and_exception_cleanup(repo, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(leases.WorkspaceLeaseCancelled):
        with _file_lease(tmp_path / 'outside.txt', cancel_event=cancel):
            pytest.fail('cancelled non-Git operation entered')
    with pytest.raises(RuntimeError, match='injected'):
        with _file_lease(repo / 'a.txt'):
            fds = leases.workspace_lease_fds()
            raise RuntimeError('injected')
    _closed(fds)
    _clean()


@pytest.mark.parametrize('cancel_wait', [False, True])
@pytest.mark.parametrize('holder_api', ['workspace_file_write_lease', 'workspace_write_lease'])
def test_wait_release_and_cancellation_close_partial_fds(repo, monkeypatch, cancel_wait, holder_api):
    import fcntl
    attempted, cancel, acquired = threading.Event(), threading.Event(), threading.Event()
    flock = fcntl.flock

    def probe(fd, flags):
        try:
            return flock(fd, flags)
        except BlockingIOError:
            attempted.set()
            raise

    def waiter():
        if cancel_wait:
            with pytest.raises(leases.WorkspaceLeaseCancelled):
                with _file_lease(repo / 'a.txt', timeout=5, cancel_event=cancel):
                    acquired.set()
        else:
            with _file_lease(repo / 'a.txt', timeout=5, cancel_event=cancel):
                acquired.set()

    with _process_holder(repo / 'a.txt', holder_api) as child:
        monkeypatch.setattr(fcntl, 'flock', probe)
        worker, errors = _thread(waiter)
        try:
            assert attempted.wait(5)
        finally:
            if cancel_wait:
                cancel.set()
            else:
                child.kill()
                child.wait(timeout=5)
            _join(worker, errors)
        assert acquired.is_set() is not cancel_wait
        _clean()
    with leases.workspace_write_lease(repo):
        assert leases.workspace_lease_fds()
    _clean()


def test_inherited_both_descriptors_fence_until_child_exits(repo):
    child = None
    try:
        with _file_lease(repo / 'a.txt'):
            fds = leases.workspace_lease_fds()
            assert len(fds) >= 2
            child = subprocess.Popen(
                [sys.executable, '-c', 'import sys; print("ready",flush=True); sys.stdin.read()'],
                pass_fds=fds, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == 'ready'
        _closed(fds)
        _clean()
        with _file_lease(repo / 'b.txt'):
            assert len(leases.workspace_lease_fds()) >= 2
        for api, target in ((_file_lease, repo / 'a.txt'),
                            (leases.workspace_write_lease, repo),
                            (leases.workspace_read_lease, repo)):
            with pytest.raises(leases.WorkspaceBusyError):
                with api(target):
                    pytest.fail('child lost inherited file or whole-checkout fence')
    finally:
        if child is not None:
            child.stdin.close()
            child.wait(timeout=5)
            child.stdout.close()
    with leases.workspace_write_lease(repo):
        assert leases.workspace_lease_fds()
    _clean()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork cleanup')
def test_fork_drops_file_ownership_and_does_not_close_reused_fds(repo):
    read_fd, write_fd = os.pipe()
    child_pid = None
    try:
        with _file_lease(repo / 'a.txt'):
            fds = leases.workspace_lease_fds()
            child_pid = os.fork()
            if child_pid == 0:
                os.close(read_fd)
                try:
                    _clean()
                    _closed(fds)
                    with pytest.raises(leases.WorkspaceBusyError):
                        with _file_lease(repo / 'a.txt'):
                            raise AssertionError('fork inherited ownership')
                    with _file_lease(repo / 'b.txt'):
                        assert len(leases.workspace_lease_fds()) >= 2
                    reused = os.open(os.devnull, os.O_RDONLY)
                    if reused != fds[0]:
                        os.dup2(reused, fds[0])
                        os.close(reused)
                    reused = fds[0]
                    os.write(write_fd, b'checked')
                except BaseException as error:
                    os.write(write_fd, repr(error).encode())
                    os._exit(1)
            else:
                assert os.read(read_fd, len(b'checked')) == b'checked'
        if child_pid == 0:
            try:
                os.fstat(reused)
                os.close(reused)
                _clean()
                os.write(write_fd, b'clean')
                os._exit(0)
            except BaseException as error:
                os.write(write_fd, repr(error).encode())
                os._exit(1)
        os.close(write_fd)
        write_fd = None
        assert os.read(read_fd, 4096) == b'clean'
        _, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert os.waitstatus_to_exitcode(status) == 0
        _closed(fds)
        _clean()
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if child_pid:
            os.waitpid(child_pid, 0)


def test_unsafe_target_lock_metadata_releases_barrier(repo, tmp_path):
    lock_dir = repo / '.git' / 'tofu-workspace-leases'
    lock_dir.mkdir()
    victim = tmp_path / 'victim'
    victim.write_text('keep')
    target_lock = lock_dir / (hashlib.sha256(os.fsencode(str(repo / 'a.txt'))).hexdigest() + '.lock')
    target_lock.symlink_to(victim)
    with pytest.raises(leases.WorkspaceBusyError):
        with _file_lease(repo / 'a.txt'):
            pytest.fail('unsafe target lock admitted')
    assert victim.read_text() == 'keep'
    _clean()
    with leases.workspace_write_lease(repo):
        assert leases.workspace_lease_fds()
    _clean()


def test_registry_and_descriptor_growth_only_tracks_active_holders(repo):
    for index in range(40):
        with _file_lease(repo / f'new-{index}.txt'):
            assert len(leases.workspace_lease_fds()) >= 2
            assert len(leases._registry) == len(leases.workspace_lease_fds())
            assert leases._active_descriptors == set(leases.workspace_lease_fds())
            fds = leases.workspace_lease_fds()
        _closed(fds)
        _clean()


@pytest.mark.parametrize('route', ['write', 'append', 'diff', 'diffs', 'insert', 'inserts', 'upload'])
def test_file_tools_overlap_different_target_and_hold_both_fds_through_publish(repo, monkeypatch, route):
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    original = _ops._atomic_write_bytes
    observed = []

    def publish(target, data, **kwargs):
        assert len(leases.workspace_lease_fds()) >= 2
        observed.append(os.path.realpath(target))
        return original(target, data, **kwargs)

    monkeypatch.setattr(_ops, '_atomic_write_bytes', publish)
    base = str(repo)

    def write_b():
        if route == 'write':
            result = _ops.tool_write_file(base, 'b.txt', 'changed\n')
        elif route == 'append':
            result = _ops.tool_write_file(base, 'b.txt', 'changed\n', append=True)
        elif route == 'diff':
            result = _ops.tool_apply_diff(base, 'b.txt', 'b', 'changed')
        elif route == 'diffs':
            result = _ops.tool_apply_diffs(base, [{'path': 'b.txt', 'search': 'b', 'replace': 'changed'}])
        elif route == 'insert':
            result = _ops.tool_insert_content(base, 'b.txt', 'b', 'changed')
        elif route == 'inserts':
            result = _ops.tool_insert_contents(base, [{'path': 'b.txt', 'anchor': 'b', 'content': 'changed'}])
        else:
            result = _ops.save_uploaded_file(base, 'b.txt', b'changed\n', on_conflict='overwrite')
        if isinstance(result, dict):
            assert result['ok'], result
        else:
            assert '1/1' in result, result

    with _process_holder(repo / 'a.txt', 'workspace_write_lease'):
        # Old whole-checkout writers still prevent file tools before preimages.
        result = _ops.tool_write_file(base, 'b.txt', 'blocked')
        assert result['error_kind'] == 'workspace-busy'
        assert not observed
    with _process_holder(repo / 'a.txt'):
        write_b()
    assert observed == [str(repo / 'b.txt')]
    _clean()


def test_batch_busy_preflight_applies_nothing_and_closes_other_targets(repo, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('busy batch reached preimage or publication')

    monkeypatch.setattr(_ops, '_read_original_text', forbidden)
    edits = [{'path': name, 'search': name[0], 'replace': 'changed'}
             for name in ('a.txt', 'b.txt')]
    with _process_holder(repo / 'b.txt'):
        result = _ops.tool_apply_diffs(str(repo), edits)
        assert '[workspace-busy]' in result
        assert 'No edits applied' in result
        assert (repo / 'a.txt').read_text() == 'a\n'
        assert (repo / 'b.txt').read_text() == 'b\n'
        _clean()


def test_upload_rename_keeps_discovered_destination_protected(repo, monkeypatch):
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    with _process_holder(repo / 'b (1).txt'):
        result = _ops.save_uploaded_file(str(repo), 'b.txt', b'new\n')
        assert result['ok'] is False
        assert result['error_kind'] == 'workspace-busy'
        assert not (repo / 'b (1).txt').exists()
    _clean()


def test_write_tool_uses_file_admission_without_checkout_holder(repo, monkeypatch):
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    original = _ops._atomic_write_bytes

    def publish(target, data, **kwargs):
        assert len(leases.workspace_lease_fds()) >= 2
        return original(target, data, **kwargs)

    monkeypatch.setattr(_ops, '_atomic_write_bytes', publish)
    result = _ops.tool_write_file(str(repo), 'b.txt', 'changed\n')
    assert result['ok'], result
    _clean()


@pytest.mark.parametrize('route', ['api', 'tool'])
def test_alias_retarget_during_admission_applies_nothing(repo, monkeypatch, route):
    alias = repo / 'link.txt'
    alias.symlink_to(repo / 'a.txt')
    acquire = leases._acquire_process_lock

    def retarget(common, root, deadline, cancel_event, mode):
        fd = acquire(common, root, deadline, cancel_event, mode)
        if root == str(repo):
            alias.unlink()
            alias.symlink_to(repo / 'b.txt')
        return fd

    monkeypatch.setattr(leases, '_acquire_process_lock', retarget)
    if route == 'api':
        with pytest.raises(leases.WorkspaceBusyError, match='target changed'):
            with _file_lease(alias):
                pytest.fail('retargeted alias admitted')
    else:
        result = _ops.tool_write_file(str(repo), 'link.txt', 'changed\n')
        assert result['error_kind'] == 'workspace-target-changed'
        assert result['mutationApplied'] is False
    assert (repo / 'a.txt').read_text() == 'a\n'
    assert (repo / 'b.txt').read_text() == 'b\n'
    _clean()


@pytest.mark.parametrize('kind', ['directory', 'fifo'])
def test_target_type_changed_during_admission_refuses_file_scope(repo, monkeypatch, kind):
    target = repo / 'new'
    acquire = leases._acquire_process_lock

    def create_unknown(common, root, deadline, cancel_event, mode):
        fd = acquire(common, root, deadline, cancel_event, mode)
        if root == str(repo):
            if kind == 'directory':
                target.mkdir()
            else:
                os.mkfifo(target)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(leases, '_acquire_process_lock', create_unknown)
        with pytest.raises(leases.WorkspaceBusyError, match='target changed'):
            with _file_lease(target):
                pytest.fail('directory/unknown type admitted as a regular file')
    _clean()
    with _file_lease(target):
        assert leases.workspace_lease_fds()
    _clean()


def test_target_descriptor_validation_failure_closes_both_fds(repo, monkeypatch):
    opened = []
    original_open, original_stat = os.open, os.fstat

    def track_open(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def fail_target_stat(fd):
        if len(opened) == 2:
            raise OSError('injected target fstat failure')
        return original_stat(fd)

    with monkeypatch.context() as patch:
        patch.setattr(leases.os, 'open', track_open)
        patch.setattr(leases.os, 'fstat', fail_target_stat)
        with pytest.raises(leases.WorkspaceBusyError, match='injected target fstat failure'):
            with _file_lease(repo / 'a.txt'):
                pytest.fail('invalid target descriptor admitted')
    assert len(opened) == 2
    _closed(opened)
    _clean()


def test_second_file_holder_keeps_live_barrier_after_first_exits(repo):
    ready, first_released = threading.Event(), threading.Event()
    inherited = []

    def second():
        with _file_lease(repo / 'b.txt'):
            fds = leases.workspace_lease_fds()
            inherited.append(fds)
            ready.set()
            assert first_released.wait(5)
            assert leases.workspace_lease_fds() == fds
            for fd in fds:
                os.fstat(fd)

    try:
        with _file_lease(repo / 'a.txt'):
            first = leases.workspace_lease_fds()
            worker, errors = _thread(second)
            assert ready.wait(5)
            assert len(inherited[0]) >= 2
            assert set(first) & set(inherited[0])
            assert set(first) - set(inherited[0])
    finally:
        first_released.set()
        _join(worker, errors)
    _closed(first)
    _closed(inherited[0])
    _clean()


def test_file_admission_retains_freshness_refusal_before_preimage(repo, monkeypatch):
    from lib import write_freshness
    observed = []

    def stale(conv_id, target):
        observed.append((conv_id, target))
        assert len(leases.workspace_lease_fds()) >= 2
        return True

    monkeypatch.setattr(write_freshness, 'is_stale', stale)
    result = _ops.tool_write_file(str(repo), 'b.txt', 'changed\n', conv_id='lease-test')
    assert result['error_kind'] == 'write-freshness-stale'
    assert result['mutationApplied'] is False
    assert 're-read and reconcile' in result['error']
    assert 'waiting alone will not refresh' in result['error']
    assert observed == [('lease-test', str(repo / 'b.txt'))]
    assert (repo / 'b.txt').read_text() == 'b\n'
    _clean()


def test_barrier_and_target_share_one_timeout_budget(repo, monkeypatch):
    from types import SimpleNamespace
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(leases, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    original_lease = leases._lease
    observed = []

    @contextmanager
    def timed(path, mode, timeout, cancel_event, _identity=None, **binding):
        observed.append((path, timeout))
        with original_lease(path, mode, timeout, cancel_event, _identity, **binding):
            if path == str(repo):
                clock.now += 2.0
            yield

    monkeypatch.setattr(leases, '_lease', timed)
    with _file_lease(repo / 'a.txt', timeout=5):
        assert len(leases.workspace_lease_fds()) >= 2
    assert [(path, budget) for path, budget in observed
            if path in {str(repo), str(repo / 'a.txt')}] == [
        (str(repo), 5.0), (str(repo / 'a.txt'), 3.0)]
    assert all(0.0 <= budget <= 5.0 for _, budget in observed)
    _clean()


@pytest.fixture
def tracked_repo(repo, tmp_path, monkeypatch):
    from collections import OrderedDict
    from lib import write_freshness
    from lib.project_mod import modifications as mods

    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '1')
    monkeypatch.setenv('TOFU_WRITE_FRESHNESS_GATE', '1')
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    monkeypatch.setattr(write_freshness, '_tokens', OrderedDict())
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    return repo


def test_tracked_same_file_callback_runs_unlocked_and_retains_final_freshness(tracked_repo, monkeypatch):
    from lib import write_freshness
    from lib.project_mod import modifications as mods, post_commit, session_lock
    repo = tracked_repo
    seen, nested = [], []
    session = mods._get_session_dir(str(repo))

    def listener(task, entry):
        seen.append((task, leases.workspace_lease_fds(), session_lock.session_state(session)))
        if task == 'outer':
            nested.append(_ops.tool_write_file(str(repo), 'a.txt', 'last\n',
                          conv_id='file-lease-callbacks', task_id='inner'))

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])
    result = _ops.tool_write_file(str(repo), 'a.txt', 'first\n',
                                 conv_id='file-lease-callbacks', task_id='outer')
    assert result['ok'], result
    assert len(nested) == 1 and nested[0]['ok'], nested
    assert (repo / 'a.txt').read_text() == 'last\n'
    assert not write_freshness.is_stale('file-lease-callbacks', str(repo / 'a.txt'))
    assert seen == [('outer', (), None), ('inner', (), None)]
    assert [row['taskId'] for row in mods.get_modifications(str(repo))] == ['outer', 'inner']
    assert not post_commit._lanes
    _clean()


def test_tracked_cross_file_callbacks_reenter_without_file_lease_wait_cycle(tracked_repo, monkeypatch):
    from lib import write_freshness
    from lib.project_mod import modifications as mods, post_commit, session_lock
    repo = tracked_repo
    both_committed = threading.Barrier(2)
    nested, observations, outcomes = [], [], []
    session = mods._get_session_dir(str(repo))

    def listener(task, entry):
        observations.append((task, leases.workspace_lease_fds(), session_lock.session_state(session)))
        if not task.startswith('outer-'):
            return
        both_committed.wait(timeout=5)
        other = 'b.txt' if task == 'outer-a' else 'a.txt'
        nested.append(_ops.tool_write_file(str(repo), other, 'reentered-' + other,
                      conv_id='file-lease-callbacks', task_id='nested-' + task))

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])

    def write(name):
        outcomes.append(_ops.tool_write_file(str(repo), name + '.txt', 'outer-' + name,
                        conv_id='file-lease-callbacks', task_id='outer-' + name))

    left, left_errors = _thread(lambda: write('a'))
    right, right_errors = _thread(lambda: write('b'))
    _join(left, left_errors)
    _join(right, right_errors)
    assert len(outcomes) == 2 and all(result['ok'] for result in outcomes), outcomes
    assert len(nested) == 2 and all(result['ok'] for result in nested), nested
    assert all(not fds and state is None for _, fds, state in observations), observations
    assert not write_freshness.is_stale('file-lease-callbacks', str(repo / 'a.txt'))
    assert not write_freshness.is_stale('file-lease-callbacks', str(repo / 'b.txt'))
    rows = mods.get_modifications(str(repo))
    assert len(rows) == 4 and len({row['changeId'] for row in rows}) == 4
    assert not post_commit._lanes
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    for task in ('nested-outer-a', 'nested-outer-b', 'outer-a', 'outer-b'):
        assert mods.undo_task_modifications(str(repo), task)['undone'] == 1
    assert (repo / 'a.txt').read_text() == 'a\n'
    assert (repo / 'b.txt').read_text() == 'b\n'
    _clean()


@pytest.mark.parametrize('execution', ['thread', 'process'])
@pytest.mark.parametrize('phase', ['prepared', 'ack-failure'])
def test_real_cross_file_journal_contention_preserves_active_intent_and_exact_undo(
        tracked_repo, monkeypatch, execution, phase):
    import json
    import multiprocessing
    from pathlib import Path
    from lib.project_mod import modifications as mods, session_lock, write_intents

    if not hasattr(os, 'fork'):
        pytest.skip('requires POSIX fork/flock')
    ctx = multiprocessing.get_context('fork')
    repo = tracked_repo
    stopped, release, faulted = (ctx.Event() for _ in range(3))
    session = Path(mods._get_session_dir(str(repo)))
    monkeypatch.setattr(session_lock, '_wait_seconds', lambda: 0.05)
    prepare = write_intents.IntentStore.prepare
    acknowledge = write_intents.IntentStore.acknowledge

    def paused_prepare(store, **kwargs):
        record = prepare(store, **kwargs)
        if phase == 'prepared' and kwargs.get('task_id') == 'first':
            stopped.set()
            assert release.wait(10)
        return record

    def interrupted_ack(store, record):
        if phase == 'ack-failure' and record['taskId'] == 'first' and not faulted.is_set():
            stopped.set()
            assert release.wait(10)
            faulted.set()
            raise OSError('injected once after durable journal publication')
        return acknowledge(store, record)

    monkeypatch.setattr(write_intents.IntentStore, 'prepare', paused_prepare)
    monkeypatch.setattr(write_intents.IntentStore, 'acknowledge', interrupted_ack)
    first_results, children, pipes = [], [], []

    def first_write():
        return _ops.tool_write_file(str(repo), 'a.txt', 'A-new\n',
                                   conv_id='file-lease-journal', task_id='first')

    if execution == 'thread':
        worker, errors = _thread(lambda: first_results.append(first_write()))
    else:
        receive, send = ctx.Pipe(duplex=False)
        pipes.extend((receive, send))

        def first_process():
            receive.close()
            try:
                send.send(first_write())
            except BaseException as error:
                send.send({'childError': repr(error)})
            finally:
                send.close()

        child = ctx.Process(target=first_process, daemon=True)
        children.append(child)
        child.start()
        send.close()

    try:
        try:
            assert stopped.wait(5), 'first writer did not reach real durability boundary'
            pending = sorted((session / 'write-intents-v1').glob('*.json'))
            assert len(pending) == 1
            intent_bytes = pending[0].read_bytes()
            intent = json.loads(intent_bytes)
            stage = repo / intent['stageName']
            stage_bytes = stage.read_bytes() if stage.exists() else None
            assert (repo / 'a.txt').read_bytes() == (b'a\n' if phase == 'prepared' else b'A-new\n')
            busy = _ops.tool_write_file(str(repo), 'b.txt', 'B-new\n',
                                       conv_id='file-lease-journal', task_id='second')
            assert busy['ok'] is False and busy['errorKind'] == 'journal-busy', busy
            assert busy['mutationApplied'] is False
            assert (repo / 'b.txt').read_bytes() == b'b\n'
            assert pending[0].read_bytes() == intent_bytes
            assert (stage.read_bytes() if stage.exists() else None) == stage_bytes
        finally:
            release.set()
            if execution == 'thread':
                _join(worker, errors)
            else:
                assert receive.poll(5), 'first process failed to quiesce'
                first_results.append(receive.recv())
                child.join(5)
                assert child.exitcode == 0

        assert len(first_results) == 1 and first_results[0].get('ok'), first_results
        if phase == 'ack-failure':
            assert faulted.is_set() and pending[0].exists()
        committed = _ops.tool_write_file(str(repo), 'b.txt', 'B-new\n',
                                        conv_id='file-lease-journal', task_id='second')
        assert committed['ok'], committed
        rows = mods.get_modifications(str(repo))
        assert [(row['path'], row['taskId']) for row in rows] == [('a.txt', 'first'), ('b.txt', 'second')]
        assert rows[0]['changeId'] == intent['changeId']
        assert len({row['changeId'] for row in rows}) == 2
        assert not list((session / 'write-intents-v1').glob('*.json'))
        assert not list(repo.glob('.tofu_atomic_*'))
        assert mods.undo_task_modifications(str(repo), 'second')['undone'] == 1
        assert mods.undo_task_modifications(str(repo), 'first')['undone'] == 1
        assert (repo / 'a.txt').read_bytes() == b'a\n'
        assert (repo / 'b.txt').read_bytes() == b'b\n'
        _clean()
    finally:
        release.set()
        for child in children:
            child.join(1)
            if child.is_alive():
                child.terminate()
                child.join(5)
        for pipe in pipes:
            pipe.close()


def test_tracked_tool_keeps_external_session_notification_release_order(tracked_repo, monkeypatch):
    from lib.project_mod import modifications as mods, post_commit, session_lock
    repo = tracked_repo
    session = mods._get_session_dir(str(repo))
    seen = []

    def listener(task, entry):
        seen.append((task, leases.workspace_lease_fds(), session_lock.session_state(session)))

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])
    with session_lock.locked_session(session) as state:
        result = _ops.tool_write_file(str(repo), 'a.txt', 'changed\n', task_id='outer-session')
        assert result['ok'], result
        assert not seen
        assert state['afterRelease']
        _clean()
    assert seen == [('outer-session', (), None)]
    assert not post_commit._lanes
    _clean()


def test_committed_notification_flushes_unlocked_after_tool_exception(tracked_repo, monkeypatch):
    from lib.project_mod import modifications as mods, post_commit, session_lock
    repo = tracked_repo
    session = mods._get_session_dir(str(repo))
    seen = []

    def listener(task, entry):
        seen.append((task, leases.workspace_lease_fds(), session_lock.session_state(session)))

    monkeypatch.setattr(mods, '_live_change_listeners', [listener])

    @_ops._with_workspace_writer
    def commit_then_fail(base, rel_path):
        target = str(repo / rel_path)
        _ops._write_with_undo(target, 'committed\n', base, rel_path, 'a\n',
                             tracked=True, task_id='committed-before-error')
        raise RuntimeError('injected after acknowledged commit')

    with pytest.raises(RuntimeError, match='acknowledged commit'):
        commit_then_fail(str(repo), 'a.txt')
    assert seen == [('committed-before-error', (), None)]
    assert (repo / 'a.txt').read_text() == 'committed\n'
    assert len(mods.get_modifications(str(repo))) == 1
    assert not post_commit._lanes
    assert mods.undo_task_modifications(str(repo), 'committed-before-error')['undone'] == 1
    assert (repo / 'a.txt').read_text() == 'a\n'
    _clean()


@pytest.mark.parametrize('batch', [False, True])
def test_stale_read_without_holder_requires_reread_not_lock_wait(repo, monkeypatch, batch):
    from lib import write_freshness
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    conversation = 'freshness-' + str(repo)
    target = str(repo / 'b.txt')
    write_freshness.record(conversation, target)
    (repo / 'b.txt').write_text('peer\n')

    def edit():
        if batch:
            return _ops.tool_apply_diffs(str(repo), [
                {'path': 'a.txt', 'search': 'a', 'replace': 'changed'},
                {'path': 'b.txt', 'search': 'peer', 'replace': 'reconciled'},
            ], conv_id=conversation)
        return _ops.tool_write_file(str(repo), 'b.txt', 'reconciled\n', conv_id=conversation)

    _clean()
    result = edit()
    if batch:
        assert '[write-freshness-stale]' in result
        assert 'No edits applied' in result
        assert 'retry after the writer finishes' not in result
    else:
        assert result['error_kind'] == 'write-freshness-stale'
        assert result['mutationApplied'] is False
    assert (repo / 'a.txt').read_text() == 'a\n'
    assert (repo / 'b.txt').read_text() == 'peer\n'
    _clean()
    write_freshness.record(conversation, target)
    result = edit()
    assert ('2/2' in result) if batch else result['ok']
    assert (repo / 'b.txt').read_text() == 'reconciled\n'
    _clean()


def test_changed_target_is_not_reported_as_an_active_writer(repo, monkeypatch):
    from lib.project_mod import workspace_lease as admission
    original = admission.workspace_file_write_lease
    observed = []

    @contextmanager
    def retarget(path, **kwargs):
        observed.append(path)
        with original(path, **kwargs):
            (repo / 'b.txt').unlink()
            (repo / 'b.txt').symlink_to(repo / 'a.txt')
            yield

    monkeypatch.setattr(admission, 'workspace_file_write_lease', retarget)
    @_ops._with_workspace_writer
    def mutation(base, rel_path):
        pytest.fail('changed target reached mutation body')

    result = mutation(str(repo), 'b.txt')
    assert observed == [str(repo / 'b.txt')], result
    assert result.get('error_kind') == 'workspace-target-changed', result
    assert result['mutationApplied'] is False
    assert 're-resolve and re-read' in result['error']
    assert (repo / 'a.txt').read_text() == 'a\n'
    _clean()


@pytest.mark.parametrize('timeout', [None, 5000])
def test_file_admission_accepts_unlimited_or_exact_long_wait(repo, timeout):
    with _file_lease(repo / 'a.txt', timeout=timeout):
        assert leases.workspace_lease_fds()
    _clean()
