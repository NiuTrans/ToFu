"""Real disposable children specify ranking cancellation, limits and join debt."""

import sys
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tofu_search import local_rerank as module

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux CPU worker"),
]


@pytest.fixture
def worker(tmp_path, monkeypatch, request):
    script = tmp_path / "worker.py"
    trace = tmp_path / "pid"
    script.write_text("""import json, os, signal, subprocess, sys, time
from pathlib import Path
time.sleep(float(sys.argv[2]))
data=json.load(sys.stdin)
Path(sys.argv[1]).write_text(str(os.getpid()))
mode=data['query']
if mode=='hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:time.sleep(.01)
if mode=='descendant':
    child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)'])
    Path(sys.argv[1]+'.child').write_text(str(child.pid))
    time.sleep(.1)
if mode=='flood':
    sys.stdout.write('x'*100000)
elif mode=='invalid':
    print(json.dumps({'ok':True,'scores':[float('nan')]}))
else:
    print(json.dumps({'ok':True,'scores':[.5]*len(data['documents'])}))
""")
    ranker = module.LocalReranker(tmp_path, threads=1, max_parallel=1, max_rss_mb=4096)
    startup_delay = str(getattr(request, 'param', 0))
    monkeypatch.setattr(
        ranker, "_command", lambda: [sys.executable, str(script), str(trace), startup_delay])
    return ranker, trace


def wait_for(path):
    deadline = time.monotonic() + 5
    while not path.exists():
        assert time.monotonic() < deadline, "owned worker did not start"
        time.sleep(0.01)


def is_live(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


def expire_when_ready(monkeypatch, ready, *, timeout):
    """Drive the tested deadline after readiness; leave cleanup clocks running."""
    started = time.monotonic()
    expired_at = None

    def monotonic():
        nonlocal expired_at
        now = time.monotonic()
        if expired_at is None:
            assert now - started < 5, 'owned worker did not reach the timeout boundary'
            if ready():
                expired_at = now
        if expired_at is None:
            return started
        return started + timeout + (now - expired_at)

    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=monotonic))


def test_success_reaps_child_and_releases_the_reservation(worker):
    ranker, trace = worker
    assert ranker.score("normal", ["a", "b"]) == [0.5, 0.5]
    assert not is_live(int(trace.read_text()))
    assert ranker.score("normal", ["c"]) == [0.5]


def test_cancellation_kills_a_term_ignoring_child_before_return(worker):
    ranker, trace = worker
    stop = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(ranker.score, "hang", ["a"], cancel_event=stop)
        wait_for(trace)
        stop.set()
        with pytest.raises(CancelledError):
            future.result(timeout=4)
    assert not is_live(int(trace.read_text()))


@pytest.mark.parametrize('worker', [0, 1], indirect=True)
def test_timeout_retires_descendants_after_the_leader_exits(worker, monkeypatch):
    ranker, trace = worker
    descendant = Path(str(trace) + '.child')
    timeout = 0.4
    expire_when_ready(monkeypatch, lambda: (
        descendant.exists() and not is_live(int(trace.read_text()))), timeout=timeout)
    with pytest.raises(TimeoutError):
        ranker.score("descendant", ["a"], timeout=timeout)
    assert not is_live(int(trace.read_text()))
    assert not is_live(int(descendant.read_text()))


@pytest.mark.parametrize("query,kind", [("flood", "output_limit"), ("invalid", "invalid_scores")])
def test_unusable_outputs_never_escape_and_worker_is_reaped(worker, query, kind):
    ranker, trace = worker
    with pytest.raises(module.LocalRerankError) as error:
        ranker.score(query, ["a"])
    assert error.value.kind == kind
    assert not is_live(int(trace.read_text()))


def test_default_calls_start_together_whatever_the_host_reports(tmp_path, monkeypatch):
    started = tmp_path / "started"
    started.mkdir()
    script = tmp_path / "barrier.py"
    script.write_text("""import json, sys, time
from pathlib import Path
data=json.load(sys.stdin)
root=Path(sys.argv[1])
(root/data['query']).write_text('started')
deadline=time.monotonic()+10
while len(list(root.iterdir()))<int(sys.argv[2]):
    if time.monotonic()>deadline:sys.exit(3)
    time.sleep(.01)
print(json.dumps({'ok':True,'scores':[.5]*len(data['documents'])}))
""")
    monkeypatch.setattr(
        module,
        "probe_resources",
        lambda _path: SimpleNamespace(effective_cpu_count=1, effective_memory_available_mb=1),
    )
    calls = 4
    ranker = module.LocalReranker(tmp_path)
    monkeypatch.setattr(
        ranker, "_command", lambda: [sys.executable, str(script), str(started), str(calls)]
    )
    with ranker, ThreadPoolExecutor(calls) as pool:
        futures = [
            pool.submit(ranker.score, str(index), ["a"], timeout=20) for index in range(calls)
        ]
        assert [future.result(timeout=30) for future in futures] == [[0.5]] * calls


def test_rss_over_budget_retires_the_worker(worker, monkeypatch):
    ranker, trace = worker
    monkeypatch.setattr(module, "_resident_bytes", lambda pid: ranker.max_rss_bytes + 1)
    with pytest.raises(module.LocalRerankError) as error:
        ranker.score("hang", ["a"])
    assert error.value.kind == "memory_limit"
    if trace.exists():
        assert not is_live(int(trace.read_text()))


def test_waiting_for_capacity_respects_its_own_cancellation(worker):
    ranker, trace = worker
    first_stop = threading.Event()
    second_stop = threading.Event()
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(ranker.score, "hang", ["a"], cancel_event=first_stop)
        wait_for(trace)
        second = pool.submit(ranker.score, "normal", ["b"], cancel_event=second_stop)
        second_stop.set()
        try:
            with pytest.raises(CancelledError):
                second.result(timeout=2)
        finally:
            first_stop.set()
        with pytest.raises(CancelledError):
            first.result(timeout=4)


def test_stop_during_retirement_fences_the_completed_scores(worker, monkeypatch):
    ranker, _trace = worker
    stop = threading.Event()
    retire = module.retire_process_group

    def stopping_retirement(*args, **kwargs):
        retire(*args, **kwargs)
        stop.set()

    monkeypatch.setattr(module, "retire_process_group", stopping_retirement)
    with pytest.raises(CancelledError):
        ranker.score("normal", ["a"], cancel_event=stop)


def test_failed_retirement_retains_join_debt_until_close_retries(worker, monkeypatch):
    ranker, _trace = worker
    retire = module.retire_process_group

    def refused_retirement(*args, **kwargs):
        raise RuntimeError("observation unavailable")

    monkeypatch.setattr(module, "retire_process_group", refused_retirement)
    with pytest.raises(module.LocalRerankCleanupError):
        ranker.score("normal", ["a"])
    assert not ranker._slots.acquire(blocking=False)
    monkeypatch.setattr(module, "retire_process_group", retire)
    ranker.close()
    assert ranker._slots.acquire(blocking=False)
    ranker._slots.release()
    with pytest.raises(CancelledError):
        ranker.score("normal", ["a"])


def test_close_cancels_and_joins_an_active_call(worker):
    ranker, trace = worker
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(ranker.score, "hang", ["a"])
        wait_for(trace)
        ranker.close()
        with pytest.raises(CancelledError):
            future.result(timeout=1)
    assert not is_live(int(trace.read_text()))


def test_start_failure_releases_capacity_without_claiming_success(worker, monkeypatch):
    ranker, _trace = worker
    popen = module.subprocess.Popen

    def refused_start(*_args, **_kwargs):
        raise OSError("physical worker unavailable")

    monkeypatch.setattr(module.subprocess, "Popen", refused_start)
    with pytest.raises(module.LocalRerankError) as error:
        ranker.score("normal", ["a"])
    assert error.value.kind == "worker_start_failed"
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    assert ranker.score("normal", ["a"]) == [0.5]


@pytest.mark.parametrize("boundary", ["input_file", "output_pipe"])
def test_io_failure_retires_any_child_and_allows_lexical_fallback(worker, monkeypatch, boundary):
    ranker, _trace = worker
    children = []
    popen = module.subprocess.Popen
    owner, name = (
        (module.tempfile, "TemporaryFile")
        if boundary == "input_file"
        else (module.os, "set_blocking")
    )
    original = getattr(owner, name)

    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        children.append(process)
        return process

    def unavailable(*_args, **_kwargs):
        raise OSError("owned input/output unavailable")

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    monkeypatch.setattr(owner, name, unavailable)
    with pytest.raises(module.LocalRerankError) as error:
        ranker.score("normal", ["a"])
    assert error.value.kind == "worker_io_failed"
    assert all(process.poll() is not None for process in children)
    monkeypatch.setattr(owner, name, original)
    assert ranker.score("normal", ["a"]) == [0.5]


@pytest.mark.parametrize('worker', [0, 1], indirect=True)
def test_live_cleanup_debt_is_retained_until_a_successful_retry(worker, monkeypatch):
    ranker, trace = worker
    retire = module.retire_process_group
    timeout = 0.2
    expire_when_ready(monkeypatch, lambda: trace.exists() and bool(trace.read_text()),
                      timeout=timeout)

    def refused_retirement(*_args, **_kwargs):
        raise RuntimeError("temporary retirement observation failure")

    monkeypatch.setattr(module, "retire_process_group", refused_retirement)
    try:
        with pytest.raises(module.LocalRerankCleanupError):
            ranker.score("hang", ["a"], timeout=timeout)
        assert is_live(int(trace.read_text()))
        assert len(ranker._join_debt) == 1
        with pytest.raises(module.LocalRerankCleanupError):
            ranker.score("normal", ["b"])
    finally:
        monkeypatch.setattr(module, "retire_process_group", retire)
        ranker.close()
    assert not is_live(int(trace.read_text()))
