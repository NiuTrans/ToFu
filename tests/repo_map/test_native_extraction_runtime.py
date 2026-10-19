"""Explicit runtime image loans preserve task authority, cancellation and cleanup."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
import os
import shutil
import subprocess
import threading

import pytest
from test_native_extraction_stream import stream as stream, assert_joined, observe_children
from test_native_extraction_owned import observe_images, blocked_sources, observe_first_ast

pytestmark = pytest.mark.unit
OWNER = 900001


def runtime_for(scanner):
    from lib.repo_map._native_owned import RuntimeStreamImage
    return RuntimeStreamImage(scanner.binary, expected_sha256=scanner.expected_sha256)


def scope_for(scanner, root, runtime, *, owner=OWNER, cancellation=None):
    from lib.repo_map._native_owned import OwnedStreamScope
    return OwnedStreamScope(root, owner, scanner.binary, scanner.grammar_directory,
                            expected_sha256=scanner.expected_sha256, limits=scanner.limits,
                            cancellation=cancellation, runtime_image=runtime)


def test_runtime_reuses_only_code_across_owners_and_roots(stream, tmp_path, monkeypatch):
    scanner, root = stream
    second = tmp_path/'second'; second.mkdir()
    subprocess.run(['git', 'init', '-q', str(second)], check=True)
    (root/'a.py').write_text('def first(): pass\n')
    (second/'b.py').write_text('def second(): pass\n')
    images = observe_images(monkeypatch)
    with runtime_for(scanner) as runtime:
        assert runtime.retained_image_bytes() == 0
        for directory, owner, name in [(root, OWNER, 'first'), (second, OWNER+1, 'second')]*3:
            with scope_for(scanner, directory, runtime, owner=owner) as scope:
                with pytest.raises(PermissionError): scope.scan(owner+2)
                result = scope.scan(owner)
                assert [tag.name for tags in result.tags.values() for tag in tags] == [name]
                assert scope.retained_image_bytes(owner) == 0
            assert runtime.retained_image_bytes() == scanner.binary.stat().st_size
            assert len(images) == 1 and not images[0]._image.closed
    assert runtime.retained_image_bytes() == 0 and images[0]._image.closed


def test_idle_tasks_neither_allocate_nor_delay_runtime_shutdown(stream, monkeypatch):
    scanner, root = stream
    images = observe_images(monkeypatch)
    runtime = runtime_for(scanner)
    scope = scope_for(scanner, root, runtime)
    runtime.close(); runtime.close()
    with pytest.raises(RuntimeError, match='closed'): scope.scan(OWNER)
    scope.close(OWNER)
    assert not images and runtime.retained_image_bytes() == 0


def test_tasks_share_lazy_image_under_concurrency(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'a.js').write_text('function concurrent() { return other; }')
    expected = asdict(outline.scan(str(root)))
    images = observe_images(monkeypatch)
    children = observe_children(monkeypatch)
    barrier = threading.Barrier(8)
    with runtime_for(scanner) as runtime:
        def run(_):
            with scope_for(scanner, root, runtime) as scope:
                barrier.wait(timeout=5)
                return asdict(scope.scan(OWNER))
        with ThreadPoolExecutor(max_workers=8) as pool:
            assert list(pool.map(run, range(8))) == [expected]*8
        assert len(images) == 1 and not images[0]._image.closed
    assert_joined(children)
    assert images[0]._image.closed


def test_runtime_close_cancels_borrower_before_image_retirement(stream, monkeypatch):
    from lib.repo_map._native_stream import DiscoveryCancelled
    scanner, root = stream
    blocked_sources(root)
    entered = observe_first_ast(monkeypatch)
    children = observe_children(monkeypatch)
    runtime = runtime_for(scanner)
    scope = scope_for(scanner, root, runtime)
    errors = []
    def run():
        try: scope.scan(OWNER, timeout=10)
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=run); worker.start()
    try:
        assert entered.wait(timeout=5)
        wait, close = runtime._condition.wait, runtime._image.close
        counts = dict(wait=0, close=0)
        def interrupted_wait(*args):
            counts['wait'] += 1
            if counts['wait'] <= 2: raise KeyboardInterrupt('runtime wait interruption')
            return wait(*args)
        def interrupted_close():
            counts['close'] += 1
            if counts['close'] <= 2: raise KeyboardInterrupt('image close interruption')
            assert_joined(children)
            return close()
        monkeypatch.setattr(runtime._condition, 'wait', interrupted_wait)
        monkeypatch.setattr(runtime._image, 'close', interrupted_close)
        with pytest.raises(KeyboardInterrupt): runtime.close()
        assert counts['close'] >= 3 and runtime.retained_image_bytes() == 0
    finally:
        runtime.close(); scope.close(OWNER); worker.join(timeout=5)
    assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], DiscoveryCancelled)
    assert_joined(children)


def test_task_shutdown_does_not_cancel_other_borrowers(stream, tmp_path, monkeypatch):
    from lib.repo_map._native_stream import DiscoveryCancelled
    scanner, root = stream
    blocked_sources(root)
    second = tmp_path/'independent'; second.mkdir()
    subprocess.run(['git', 'init', '-q', str(second)], check=True)
    (second/'b.py').write_text('def independent(): pass\n')
    entered = observe_first_ast(monkeypatch)
    images = observe_images(monkeypatch)
    children = observe_children(monkeypatch)
    runtime = runtime_for(scanner)
    blocked = scope_for(scanner, root, runtime)
    other = scope_for(scanner, second, runtime, owner=OWNER+1)
    errors = []
    def run():
        try: blocked.scan(OWNER, timeout=10)
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=run); worker.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(PermissionError): blocked.close(OWNER+1)
        blocked.close(OWNER)
        assert other.scan(OWNER+1).tags['b.py'][0].name == 'independent'
        assert len(images) == 1 and not images[0]._image.closed
    finally:
        blocked.close(OWNER); other.close(OWNER+1); runtime.close(); worker.join(timeout=5)
    assert not worker.is_alive() and len(errors) == 1 and isinstance(errors[0], DiscoveryCancelled)
    assert_joined(children)


def test_runtime_reentrant_close_refused_and_disposal_failure_retryable(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'a.py').write_text('def survives(): pass\n')
    runtime = runtime_for(scanner)
    scope = scope_for(scanner, root, runtime)
    extract = outline._extract_python_tags
    def observed(*args):
        with pytest.raises(RuntimeError, match='active scan'): runtime.close()
        return extract(*args)
    monkeypatch.setattr(outline, '_extract_python_tags', observed)
    scope.scan(OWNER); scope.close(OWNER)
    close = runtime._image.close
    def fail(): raise OSError('runtime disposal failure')
    monkeypatch.setattr(runtime._image, 'close', fail)
    primary = LookupError('tool failed')
    with pytest.raises(LookupError) as caught:
        with runtime: raise primary
    assert caught.value is primary and any('OSError' in note for note in primary.__notes__)
    assert runtime.retained_image_bytes() > 0
    with scope_for(scanner, root, runtime) as later:
        with pytest.raises(RuntimeError, match='closed'): later.scan(OWNER)
    monkeypatch.setattr(runtime._image, 'close', close)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: runtime.close(), range(4)))
    assert runtime.retained_image_bytes() == 0


def test_runtime_artifact_identity_and_sealed_reuse(stream, tmp_path):
    scanner, root = stream
    (root/'a.py').write_text('def sealed(): pass\n')
    binary = tmp_path/'image'; shutil.copy2(scanner.binary, binary); scanner.binary = binary
    with runtime_for(scanner) as runtime:
        with scope_for(scanner, root, runtime) as scope: scope.scan(OWNER)
        binary.write_bytes(b'replacement')
        with scope_for(scanner, root, runtime) as scope:
            assert scope.scan(OWNER).tags['a.py'][0].name == 'sealed'
        scanner.expected_sha256 = '0'*64
        with pytest.raises(ValueError, match='artifact'): scope_for(scanner, root, runtime)
        scanner.expected_sha256 = runtime.expected_sha256
    with runtime_for(scanner) as fresh, scope_for(scanner, root, fresh) as scope:
        with pytest.raises(Exception, match='digest|SHA|sha|hash'): scope.scan(OWNER)


@pytest.mark.parametrize('case', ['python_extraction','multilingual_extraction','ranking','incremental_edit','cold_scan'])
def test_actual_tasks_borrow_until_runtime_close(stream, monkeypatch, case):
    from native_domain_worker import Workload
    from native_extraction_owned import start_owned_workload
    scanner, root = stream
    reference, candidate = Workload(root, case), Workload(root, case)
    runtime = runtime_for(scanner)
    scopes = []
    def factory(project, owner, cancellation):
        result = scope_for(scanner, project, runtime, owner=owner, cancellation=cancellation)
        scopes.append(result)
        return result
    candidate.start = lambda: start_owned_workload(candidate, factory)
    images = observe_images(monkeypatch)
    try:
        for i in range(3): assert candidate.operation(i) == reference.operation(i)
    finally:
        candidate.close(); reference.close()
        assert runtime.retained_image_bytes() > 0
        runtime.close()
    assert len(images) == 1 and images[0]._image.closed
    assert len(scopes) == (3 if case == 'cold_scan' else 1)
    assert all(scope.retained_image_bytes(OWNER) == 0 for scope in scopes)
    assert candidate.runtime.task_count == reference.runtime.task_count == 0


def test_stream_import_does_not_load_other_transport_adapters(stream):
    root = Path(__file__).resolve().parents[2]
    code = """
import sys
sys.path.insert(0, 'benchmarks/repository_index')
import native_extraction_owned
assert not {'native_extraction_library', 'native_extraction_scan'} & sys.modules.keys()
assert not {'grep_ast', 'tree_sitter_language_pack'} & sys.modules.keys()
"""
    run = subprocess.run([os.environ['TOFU_BENCHMARK_PYTHON'], '-c', code], cwd=root,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
def test_failed_runtime_admission_retires_both_leases_and_can_retry(stream, monkeypatch, failure):
    import lib.repo_map._native_owned as module
    scanner, root = stream
    (root/'a.py').write_text('def retried(): pass\n')
    create = module._StreamImage
    attempts = []
    def failed(*args):
        attempts.append(True)
        if len(attempts) == 1: raise failure('image admission interrupted')
        return create(*args)
    monkeypatch.setattr(module, '_StreamImage', failed)
    with runtime_for(scanner) as runtime, scope_for(scanner, root, runtime) as scope:
        with pytest.raises(failure): scope.scan(OWNER)
        assert scope.scan(OWNER).tags['a.py'][0].name == 'retried'
    assert len(attempts) == 2 and runtime.retained_image_bytes() == 0


def test_failed_task_binding_keeps_runtime_owned_image_usable(stream, monkeypatch):
    from native_domain_worker import Workload
    from native_extraction_owned import start_owned_workload
    from lib.agent_core import execution_session
    scanner, root = stream
    work = Workload(root, 'python_extraction')
    scopes = []
    primary = LookupError('binding refused')
    with runtime_for(scanner) as runtime:
        def factory(project, owner, cancellation):
            scope = scope_for(scanner, project, runtime, owner=owner, cancellation=cancellation)
            scopes.append(scope); scope.scan(owner)
            return scope
        def refuse(*args): raise primary
        monkeypatch.setattr(execution_session, 'bind_tool_environment', refuse)
        try:
            with pytest.raises(LookupError) as caught: start_owned_workload(work, factory)
            assert caught.value is primary and work.runtime.task_count == 0
            with pytest.raises(RuntimeError, match='closed'): scopes[0].scan(OWNER)
            with scope_for(scanner, root, runtime) as next_task:
                assert next_task.scan(OWNER).tags
        finally:
            work.close()
    assert runtime.retained_image_bytes() == 0


def test_borrowed_lease_retirement_preserves_primary_failure(stream, monkeypatch):
    from lib.repo_map import outline
    scanner, root = stream
    (root/'a.py').write_text('def failed(): pass\n')
    primary = LookupError('parse failure')
    children = observe_children(monkeypatch)
    with runtime_for(scanner) as runtime, scope_for(scanner, root, runtime) as scope:
        notify = runtime._condition.notify_all
        calls = []
        def interrupted():
            calls.append(True)
            if len(calls) <= 2: raise KeyboardInterrupt('lease retirement interruption')
            return notify()
        def fail(*args): raise primary
        monkeypatch.setattr(runtime._condition, 'notify_all', interrupted)
        monkeypatch.setattr(outline, '_extract_python_tags', fail)
        with pytest.raises(LookupError) as caught: scope.scan(OWNER)
        assert caught.value is primary and len(calls) >= 3
        assert any('lease retirement interrupted' in note for note in primary.__notes__)
    assert runtime.retained_image_bytes() == 0
    assert_joined(children)
