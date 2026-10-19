"""Execution-owned sealed stream reuse, shutdown and real task cancellation."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import os
import shutil
import subprocess
import threading
import weakref

import pytest
from test_native_extraction_stream import stream as stream, assert_joined, observe_children

pytestmark = pytest.mark.unit
OWNER = 900001


def make_scope(stream, *, cancellation=None):
    from lib.repo_map._native_owned import OwnedStreamScope
    scanner, root = stream
    return OwnedStreamScope(root, OWNER, scanner.binary, scanner.grammar_directory,
                            expected_sha256=scanner.expected_sha256, limits=scanner.limits,
                            cancellation=cancellation)


def observe_images(monkeypatch):
    import lib.repo_map._native_owned as module
    original = module._StreamImage
    images = []
    def create(*args):
        image = original(*args)
        images.append(image)
        return image
    monkeypatch.setattr(module, '_StreamImage', create)
    return images


def test_owner_authority_lazy_reuse_and_closed_scope(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'sample.js').write_text('function reusable() { return referenced; }')
    expected = asdict(outline.scan(str(root)))
    images = observe_images(monkeypatch)
    scope = make_scope(stream)
    assert scope.retained_image_bytes(OWNER) == 0 and not images
    with pytest.raises(PermissionError): scope.scan(OWNER+1)
    with pytest.raises(PermissionError): scope.close(OWNER+1)
    assert not images
    with scope:
        for _ in range(3): assert asdict(scope.scan(OWNER)) == expected
        assert len(images) == 1 and not images[0]._image.closed
        assert scope.retained_image_bytes(OWNER) == scanner.binary.stat().st_size
    assert images[0]._image.closed and scope.retained_image_bytes(OWNER) == 0
    assert scope.calls == scope.discovery_calls == 3
    scope.close(OWNER)
    with pytest.raises(RuntimeError, match='closed'): scope.scan(OWNER)


def test_concurrent_scans_share_one_image_and_join_children(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'sample.rs').write_text('fn shared() { referenced(); }')
    expected = asdict(outline.scan(str(root)))
    images = observe_images(monkeypatch)
    children = observe_children(monkeypatch)
    barrier = threading.Barrier(8)
    with make_scope(stream) as scope:
        def scan(_):
            barrier.wait(timeout=5)
            return asdict(scope.scan(OWNER))
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(scan, range(8))) == [expected]*8
        assert len(images) == 1 and scope.discovery_calls == 8
    assert images[0]._image.closed
    assert_joined(children)


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_interrupted_or_failed_image_admission_is_retryable(stream, monkeypatch, failure):
    import lib.repo_map._native_owned as module
    scanner, root = stream
    (root/'sample.py').write_text('def recovered(): pass\n')
    original = module._StreamImage
    calls = []
    def create(*args):
        calls.append(True)
        if len(calls) == 1: raise failure('injected artifact admission failure')
        return original(*args)
    monkeypatch.setattr(module, '_StreamImage', create)
    with make_scope(stream) as scope:
        with pytest.raises(failure): scope.scan(OWNER)
        assert scope.retained_image_bytes(OWNER) == 0
        assert scope.scan(OWNER).tags['sample.py'][0].name == 'recovered'
    assert len(calls) == 2 and scope.retained_image_bytes(OWNER) == 0


def blocked_sources(root):
    (root/'a.py').write_text('def completed(): pass\n')
    blocked = root/'z.py'
    blocked.write_text('')
    subprocess.run(['git', '-C', str(root), 'add', 'z.py'], check=True)
    blocked.unlink(); os.mkfifo(blocked)


def observe_first_ast(monkeypatch):
    from lib.repo_map import outline
    entered = threading.Event()
    original = outline._extract_python_tags
    def observed(name, text):
        value = original(name, text)
        entered.set()
        return value
    monkeypatch.setattr(outline, '_extract_python_tags', observed)
    return entered


def test_close_cancels_and_joins_blocked_scan_despite_repeated_interrupts(stream, monkeypatch):
    from lib.repo_map._native_stream import DiscoveryCancelled
    scanner, root = stream
    blocked_sources(root)
    entered = observe_first_ast(monkeypatch)
    children = observe_children(monkeypatch)
    scope = make_scope(stream)
    errors = []
    def scan():
        try: scope.scan(OWNER, timeout=10)
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=scan)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        wait, close = scope._condition.wait, scope._image.close
        counts = dict(wait=0, close=0)
        def interrupted_wait(*args):
            counts['wait'] += 1
            if counts['wait'] <= 2: raise KeyboardInterrupt('injected owner wait interrupt')
            return wait(*args)
        def interrupted_close():
            counts['close'] += 1
            if counts['close'] <= 2: raise KeyboardInterrupt('injected image close interrupt')
            return close()
        monkeypatch.setattr(scope._condition, 'wait', interrupted_wait)
        monkeypatch.setattr(scope._image, 'close', interrupted_close)
        with pytest.raises(KeyboardInterrupt): scope.close(OWNER)
        assert counts['close'] >= 3
    finally:
        scope.close(OWNER)
        worker.join(timeout=5)
    assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], DiscoveryCancelled)
    assert scope.discovery_calls == 0 and scope.retained_image_bytes(OWNER) == 0
    assert_joined(children)


def test_close_failure_retains_owner_for_retry_and_context_preserves_primary(stream, monkeypatch):
    scanner, root = stream
    (root/'sample.py').write_text('def complete(): pass\n')
    scope = make_scope(stream)
    scope.scan(OWNER)
    close = scope._image.close
    primary = LookupError('original tool failure')
    calls = []
    def failed_close():
        calls.append(True)
        if len(calls) == 1: raise OSError('injected close failure')
        return close()
    monkeypatch.setattr(scope._image, 'close', failed_close)
    with pytest.raises(LookupError) as caught:
        with scope: raise primary
    assert caught.value is primary and any('OSError' in note for note in primary.__notes__)
    assert scope.retained_image_bytes(OWNER) > 0
    with pytest.raises(RuntimeError, match='closed'): scope.scan(OWNER)
    scope.close(OWNER)
    assert scope.retained_image_bytes(OWNER) == 0


def test_active_scan_cannot_reenter_its_own_close(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'sample.py').write_text('def retained(): pass\n')
    with make_scope(stream) as scope:
        extract = outline._extract_python_tags
        def observed(*args):
            with pytest.raises(RuntimeError, match='active scan'): scope.close(OWNER)
            return extract(*args)
        monkeypatch.setattr(outline, '_extract_python_tags', observed)
        assert scope.scan(OWNER).tags['sample.py'][0].name == 'retained'
    assert scope.retained_image_bytes(OWNER) == 0


def test_primary_error_survives_repeated_scan_lease_retirement_interrupts(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'sample.py').write_text('def interrupted(): pass\n')
    scope = make_scope(stream)
    primary = LookupError('injected parser failure')
    notify = scope._condition.notify_all
    count = []
    def interrupted_notify():
        count.append(True)
        if len(count) <= 2: raise KeyboardInterrupt('injected lease retirement interrupt')
        return notify()
    def fail(*args): raise primary
    monkeypatch.setattr(scope._condition, 'notify_all', interrupted_notify)
    monkeypatch.setattr(outline, '_extract_python_tags', fail)
    children = observe_children(monkeypatch)
    with pytest.raises(LookupError) as caught:
        scope.scan(OWNER)
    assert caught.value is primary and len(count) >= 3
    assert any('lease retirement interrupted' in note for note in primary.__notes__)
    scope.close(OWNER)
    assert scope.retained_image_bytes(OWNER) == 0
    assert_joined(children)


def test_existing_scope_uses_sealed_bytes_and_next_scope_refuses_replacement(stream, tmp_path):
    scanner, root = stream
    (root/'sample.py').write_text('def sealed(): pass\n')
    binary = tmp_path/'owned-image'
    shutil.copy2(scanner.binary, binary)
    scanner.binary = binary
    with make_scope(stream) as first:
        assert first.scan(OWNER).tags['sample.py'][0].name == 'sealed'
        binary.write_bytes(b'replaced executable')
        assert first.scan(OWNER).tags['sample.py'][0].name == 'sealed'
        with make_scope(stream) as next_scope:
            with pytest.raises(ValueError, match='digest'): next_scope.scan(OWNER)
    assert first.retained_image_bytes(OWNER) == 0


@pytest.mark.parametrize('timeout', [0, 1])
def test_pre_cancel_preserves_deadline_outcome_without_sealing(stream, monkeypatch, timeout):
    from lib.repo_map._native_stream import DiscoveryCancelled
    cancellation = threading.Event(); cancellation.set()
    images = observe_images(monkeypatch)
    with make_scope(stream, cancellation=cancellation) as scope:
        with pytest.raises(DiscoveryCancelled) as caught:
            scope.scan(OWNER, timeout=timeout)
        assert caught.value.cancelled and caught.value.timed_out == (timeout == 0)
    assert not images


def test_closed_owner_does_not_wait_for_cyclic_collection(stream):
    scope = make_scope(stream)
    reference = weakref.ref(scope)
    scope.close(OWNER)
    del scope
    assert reference() is None


def test_image_accounting_failure_is_retried_before_reusing_the_image(stream, monkeypatch):
    import lib.repo_map._native_owned as module
    scanner, root = stream
    (root/'sample.py').write_text('def accounted(): pass\n')
    scope = make_scope(stream)
    stat = module.os.fstat
    failed = []
    def interrupted(descriptor):
        if scope._image is not None and descriptor == scope._image._image.fileno() and not failed:
            failed.append(True)
            raise OSError('injected image accounting failure')
        return stat(descriptor)
    monkeypatch.setattr(module.os, 'fstat', interrupted)
    with scope:
        with pytest.raises(OSError, match='accounting failure'): scope.scan(OWNER)
        assert scope.scan(OWNER).tags['sample.py'][0].name == 'accounted'
        assert scope.retained_image_bytes(OWNER) == scanner.binary.stat().st_size
    assert scope.retained_image_bytes(OWNER) == 0


def test_failed_task_resource_binding_closes_scope_and_settles_owned_task(stream, monkeypatch):
    from native_domain_worker import Workload
    from native_extraction_owned import start_owned_workload
    from lib.agent_core import execution_session
    scanner, root = stream
    work = Workload(root, 'python_extraction')
    scopes = []
    primary = LookupError('injected registration failure')
    def factory(project, owner, cancellation):
        scope = make_scope(stream, cancellation=cancellation)
        scopes.append(scope)
        scope.scan(owner)  # Prove rollback owns an acquired descriptor as well.
        return scope
    def refuse(*args): raise primary
    monkeypatch.setattr(execution_session, 'bind_tool_environment', refuse)
    with pytest.raises(LookupError) as caught:
        start_owned_workload(work, factory)
    assert caught.value is primary and work.runtime.task_count == 0
    assert scopes[0].retained_image_bytes(OWNER) == 0
    work.close()


@pytest.mark.parametrize('case', ['python_extraction','multilingual_extraction','ranking','incremental_edit','cold_scan'])
def test_real_task_resource_binding_preserves_output_and_releases_images(stream, monkeypatch, case):
    from native_domain_worker import Workload
    from native_extraction_owned import start_owned_workload
    scanner, root = stream
    reference, candidate = Workload(root, case), Workload(root, case)
    scopes = []
    def factory(project, owner, cancellation):
        assert project == root and owner == OWNER
        scope = make_scope(stream, cancellation=cancellation); scopes.append(scope)
        return scope
    candidate.start = lambda: start_owned_workload(candidate, factory)
    images = observe_images(monkeypatch)
    try:
        for i in range(2): assert candidate.operation(i) == reference.operation(i)
    finally:
        candidate.close(); reference.close()
    assert len(images) == (2 if case == 'cold_scan' else 1)
    assert all(scope.retained_image_bytes(OWNER) == 0 for scope in scopes)
    assert all(image._image.closed for image in images)
    assert candidate.runtime.task_count == reference.runtime.task_count == 0


def test_canonical_owner_cancel_stops_blocked_tool_without_success_publication(stream, monkeypatch):
    from native_domain_worker import Workload
    from native_extraction_owned import start_owned_workload
    from lib.tasks_pkg.manager.cancellation import cancel_task
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    scanner, root = stream
    work = Workload(root, 'python_extraction')
    blocked_sources(root)
    scopes = []
    def factory(project, owner, cancellation):
        scope = make_scope(stream, cancellation=cancellation); scopes.append(scope)
        return scope
    work.start = lambda: start_owned_workload(work, factory)
    work.start()
    entered = observe_first_ast(monkeypatch)
    children = observe_children(monkeypatch)
    results, failures, settlements = [], [], []
    task = work.task
    def invoke():
        try:
            results.append(work.port.invoke(task, {}, lambda *_args, **kw: settlements.append(kw['status'])))
        except BaseException as error: failures.append(error)
    worker = threading.Thread(target=invoke)
    worker.start()
    try:
        assert entered.wait(timeout=5)
        assert not cancel_task(task['id'], user_id=OWNER+1)['found']
        assert not task['abort_event'].is_set()
        assert cancel_task(task['id'], user_id=OWNER)['signalled']
        worker.join(timeout=5)
        assert not worker.is_alive() and not failures
        assert len(results) == 1 and results[0][2] and settlements == ['error']
        events = chat_task_runtime.poll(task['id'])['events']
        assert not [e for e in events if e['type'] == 'tool_result' and e.get('status') == 'done']
    finally:
        task['abort_event'].set()
        worker.join(timeout=5)
        work.close(True)
    assert not worker.is_alive() and work.runtime.task_count == 0
    assert scopes[0].retained_image_bytes(OWNER) == 0
    assert_joined(children)
