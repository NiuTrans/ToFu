"""One owned repository-domain worker through actual task/tool publication.

Entry: measure(variant, case, mode, samples). Fixtures and production ports are
shared with the retained publication benchmark. First-use timing preserves lazy
backend loading; warm clocks include publication and completion of queued audit
writes. CPU includes worker threads and reaped discovery children, with separate
raw components. Only memory workers collect GC, outside their observed cohort. No HTTP,
durable conversation, model dispatch or application extraction cache is claimed.
"""
from contextlib import contextmanager, nullcontext
import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import resource
import shutil
import statistics
import sys
import tempfile
import time
import threading
import tracemalloc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))
from domain_fixtures import CASES, OWNER, fixture
from benchmarks.repository_index.record_journal import RecordJournal
from benchmarks.process_memory_counters import process_status
from publication_port import PublishedRepositoryToolPort, TransientChatRuntime
from lib.agent_core.execution_session import bind_tool_environment
from lib.repo_map import _native_rank, rank
from lib import log as audit

CPU_SCOPE = 'worker_threads_and_reaped_children'


def rss():
    return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')


def process_identity():
    stat = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()
    return f"{Path('/proc/sys/kernel/random/boot_id').read_text().strip()}:{os.getpid()}:{stat[19]}"


def packages():
    return {name: importlib.metadata.version(name) for name in
            ('numpy', 'scipy', 'networkx', 'tree-sitter', 'tree-sitter-language-pack', 'grep-ast')}


def discovery_executables():
    """Pin PATH resolution and binary contents without retaining file buffers."""
    result = {}
    for name in ('git', 'rg'):
        path = shutil.which(name)
        if path is None:
            result[name] = None
            continue
        digest = hashlib.sha256()
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(65536), b''):
                digest.update(chunk)
        result[name] = dict(path=os.path.realpath(path), sha256=digest.hexdigest())
    return result


def settle_audit():
    queue = audit._audit_queue
    if queue is not None:
        with queue.all_tasks_done:
            if not queue.all_tasks_done.wait_for(lambda: queue.unfinished_tasks == 0, timeout=5):
                raise RuntimeError('owned repository audit publication did not settle')


@contextmanager
def require_weighted_ranking():
    """A failed optional/native computation cannot silently become benchmark work."""
    previous = rank._fallback_scores
    def refuse(*_args, **_kwargs):
        raise RuntimeError('repository admission requires weighted PageRank, not fallback scores')
    rank._fallback_scores = refuse
    try:
        yield
    finally:
        rank._fallback_scores = previous


class Workload:
    def __init__(self, root, case):
        if case not in CASES:
            raise ValueError('unknown repository integration case')
        self.root, self.case = Path(root), case
        self.files = fixture(case)
        for name, text in self.files.items():
            (self.root / name).write_text(text)
        self.runtime = TransientChatRuntime()
        self.task = self.port = None
        self.closed = False

    def start(self):
        if self.closed:
            raise RuntimeError('repository workload is closed')
        self.task = self.runtime.create(user_id=OWNER)
        session = self.task['_executionSession']
        bind_tool_environment(session, lambda: None)
        session.mark_routed(); session.mark_reserved(); session.mark_admitted()
        assert self.runtime.mark_running(self.task['id'])
        self.port = PublishedRepositoryToolPort(self.root)

    def stop(self, failed=False):
        if self.task is not None:
            task = self.task
            self.runtime.finish(task['id'], result='owned repository workload',
                                error='fixture failure' if failed else None)
            self.runtime.cleanup_stale(max_age=0)
            self.task = self.port = None
        assert self.runtime.task_count == 0
        settle_audit()

    def close(self, failed=False):
        self.stop(failed)
        self.closed = True

    def operation(self, index):
        if self.closed:
            raise RuntimeError('repository workload is closed')
        if self.case == 'incremental_edit':
            (self.root / 'module_1.js').write_text(self.files['module_1.js'] +
                f'function edited_{index}() {{ return symbol_0(); }}\n')
        if self.task is None:
            self.start()
        try:
            settlements = []
            def finalized(_task, _number, _entry, metadata, **options):
                settlements.append((metadata, options.get('status', 'done')))
            result = self.port.invoke(self.task, {'max_tokens':8192}, finalized)
            assert len(settlements) == 1 and settlements[0][1] == 'done' and not result[2]
            assert self.task['_userId'] == OWNER
            result = result, settlements[0]
        except BaseException as primary:
            try:
                self.stop(True)
            except BaseException as cleanup:
                primary.add_note('repository workload cleanup failed: ' + type(cleanup).__name__)
            raise
        if self.case == 'cold_scan':
            self.stop()
        else:
            settle_audit()
        return result


def _clock(operation, index):
    children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu = time.process_time_ns(); wall = time.perf_counter_ns()
    result = operation(index)
    wall_ns = time.perf_counter_ns() - wall; worker_cpu_ns = time.process_time_ns() - cpu
    children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    children_cpu_ns = round(((children_after.ru_utime - children_before.ru_utime)
                            + (children_after.ru_stime - children_before.ru_stime)) * 1e9)
    assert children_cpu_ns >= 0
    return result, dict(wall_ns=wall_ns, cpu_ns=worker_cpu_ns + children_cpu_ns,
                        worker_cpu_ns=worker_cpu_ns, reaped_children_cpu_ns=children_cpu_ns)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _instrumentation_state():
    monitoring = getattr(sys, 'monitoring', None)
    # A registered tool may use code-local events with no global event mask.
    # cProfile on CPython 3.12 uses monitoring and leaves getprofile() empty.
    monitored = monitoring is not None and any(
        monitoring.get_tool(tool_id) is not None or monitoring.get_events(tool_id)
        for tool_id in range(6))
    return dict(profiler_enabled=bool(sys.getprofile() is not None or sys.gettrace() is not None
                or threading.getprofile() is not None or threading.gettrace() is not None or monitored),
                tracemalloc_enabled=tracemalloc.is_tracing())


def measure(variant, case, mode, samples, *, post_exec_memory=False):
    instrumentation_before = _instrumentation_state()
    if variant not in ('reference', 'candidate') or mode not in ('warm', 'memory', 'cold'):
        raise ValueError('explicit repository variant and worker mode required')
    if type(samples) is not int or samples <= 0:
        raise ValueError('positive sample count required')
    if post_exec_memory and mode != 'memory':
        raise ValueError('post-exec memory counters require a separate memory worker')
    present = importlib.util.find_spec('tofu_repo_kernel') is not None
    if present != (variant == 'candidate'):
        raise RuntimeError('installed native package availability differs from the requested arm')
    backend_modules_before = {name: name in sys.modules for name in
                              ('tofu_repo_kernel', 'scipy', 'networkx', 'numpy')}
    if (_native_rank.available_kernel.cache_info().currsize
            or backend_modules_before['scipy'] or backend_modules_before['tofu_repo_kernel']):
        raise RuntimeError('repository first-use backend was warmed before measurement')
    identity = process_identity()
    with tempfile.TemporaryDirectory(prefix='tofu-native-repository-domain-') as directory, (
            RecordJournal() if mode == 'memory' else nullcontext()) as journal:
        work = Workload(directory, case)
        try:
            with require_weighted_ranking():
                result, first_clock = _clock(work.operation, 0)
                first = dict(first_clock, output_sha256=digest(result)); del result
                if journal is not None:
                    journal.append(first_clock)
                if mode == 'cold':
                    raw = [first_clock]; output_hash = first['output_sha256']
                    baseline_rss = None
                else:
                    for index in range(1, 21):
                        work.operation(index)
                    work.stop()
                    if mode == 'memory':
                        gc.collect()
                    baseline_rss = rss() if mode == 'memory' else None
                    raw = []; trace = hashlib.sha256()
                    for index in range(21, samples + 21):
                        result, clocks = _clock(work.operation, index)
                        if journal is None:
                            raw.append(clocks)
                        else:
                            journal.append(clocks)
                        trace.update(bytes.fromhex(digest(result))); del result, clocks
                    output_hash = trace.hexdigest()
                work.close()
                if mode == 'memory':
                    gc.collect()
                after_rss = rss() if mode == 'memory' else None
                peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024 if mode == 'memory' else None
                lifetime_peak_rss = peak_rss
                process_image_counters = None
                if post_exec_memory:
                    process_image_counters = process_status(os.getpid())
                    peak_rss = process_image_counters['VmHWM']
                    assert peak_rss > 0 and process_image_counters['Pid'] == os.getpid()
                journal_receipt = None
                if journal is not None:
                    recorded = journal.read()
                    assert recorded[0] == first_clock and len(recorded) == samples + 1
                    raw = recorded[1:]
                    journal_receipt = journal.receipt()
            selected = _native_rank.available_kernel() is not None
            assert selected == (variant == 'candidate')
            assert work.closed and work.runtime.task_count == 0
            walls = [r['wall_ns'] / 1e6 for r in raw]; cpus = [r['cpu_ns'] / 1e6 for r in raw]
            ordered = sorted(walls)
            instrumentation_after = _instrumentation_state()
            return dict(variant=variant, case=case, mode=mode, process_identity=identity,
                cpu_scope=CPU_SCOPE,
                raw_clocks=raw, wall_samples_ms=walls, cpu_samples_ms=cpus,
                p50_ms=statistics.median(walls), p95_ms=ordered[int((len(walls)-1)*.95)],
                p99_ms=ordered[int((len(walls)-1)*.99)], throughput_per_second=len(walls)*1000/sum(walls),
                cpu_ms_per_operation=statistics.mean(cpus), first_operation=first,
                output_trace_sha256=output_hash, warmup_operations=0 if mode == 'cold' else 20,
                peak_rss_bytes=peak_rss if mode == 'memory' else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
                process_lifetime_peak_rss_bytes=lifetime_peak_rss,
                process_image_counters=process_image_counters,
                memory_peak_scope='post_exec_vm_hwm_estimate' if post_exec_memory else 'process_lifetime_rusage',
                clock_record_journal=journal_receipt,
                clock_records_materialized_after_memory_counters=mode == 'memory',
                retention_baseline_rss_bytes=baseline_rss, post_cleanup_rss_bytes=after_rss,
                retained_bytes=max(0, after_rss-baseline_rss) if mode == 'memory' else None,
                joined_cleanup=True, audit_queue_settled=True, weighted_ranking_required=True,
                native_backend_selected=selected, loaded_packages=packages(),
                discovery_executables=discovery_executables(),
                backend_modules_before=backend_modules_before,
                profiler_enabled=instrumentation_before['profiler_enabled'] or instrumentation_after['profiler_enabled'],
                tracemalloc_enabled=instrumentation_before['tracemalloc_enabled'] or instrumentation_after['tracemalloc_enabled'],
                memory_separate_process=mode == 'memory',
                affinity=sorted(os.sched_getaffinity(0)))
        finally:
            primary = sys.exception()
            try:
                work.close(failed=primary is not None)
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note('repository worker cleanup failed: ' + type(cleanup).__name__)
