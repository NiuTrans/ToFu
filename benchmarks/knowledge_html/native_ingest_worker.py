"""Fresh-process current-Python/Rust upload clocks over an owned native corpus.

First upload includes lazy parser imports, sealed-library admission and binding.
Common store creation precedes clocks, except producer-loss recovery includes
creation and reopen. Source/artifact provenance brackets the complete cohort.
Upload, chunk/index commit and complete content readback are timed.
Memory workers run the complete same cohort and materialize records afterwards.
"""
from contextlib import nullcontext
import gc
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from benchmarks.knowledge_html.native_ingest_provenance import CLOCK_SCOPE, DatabaseProvenance

CASES = ('html', 'pdf', 'batch', 'cancellation', 'bounded_memory')
CPU_SCOPE = 'current process CPU plus joined descendant CPU from RUSAGE_CHILDREN'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=True).encode()).hexdigest()


def process_identity():
    ticks = Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()[19]
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()+f':{os.getpid()}:{ticks}'


def cpu():
    child = resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time_ns(), round((child.ru_utime+child.ru_stime)*1e9)


def instrumentation_state():
    monitoring = getattr(sys,'monitoring',None)
    monitored = monitoring is not None and any(
        monitoring.get_tool(i) is not None or monitoring.get_events(i) for i in range(6))
    return dict(profiler_enabled=bool(sys.getprofile() or sys.gettrace() or
                threading.getprofile() or threading.gettrace() or monitored),
                tracemalloc_enabled=tracemalloc.is_tracing())


def clock(operation, index):
    own, child = cpu(); started = time.perf_counter_ns()
    result = operation(index)
    elapsed = time.perf_counter_ns()-started; own_end, child_end = cpu()
    return result, dict(wall_ns=elapsed,worker_cpu_ns=own_end-own,
                        reaped_children_cpu_ns=child_end-child,
                        cpu_ns=own_end-own+child_end-child)


def create_binding(options):
    # Invoked only by the first accepted HTML call, inside its operation clock.
    from lib.knowledge.native_html import NativeHtmlProjector
    if options['kind'] == 'installed':
        return NativeHtmlProjector.from_library(library=Path(options['path']),
            sha256=options['sha256'],binary_bytes=options['bytes'])
    if options['kind'] == 'archive':
        return NativeHtmlProjector.from_archive(archive=Path(options['path']),
            sha256=options['sha256'],binary_bytes=options['bytes'],
            application_version=options['application_version'])
    raise ValueError('explicit HTML artifact kind required')


def fixture_inputs(case, pdf, index):
    from benchmarks.knowledge_html.check_ingest_domain import documents
    values = []
    for ordinal, (raw, name) in enumerate(documents(case, pdf)):
        suffix = (f'<!--revision {index} document {ordinal}-->' if name.endswith('.html')
                  else f'\n%revision {index} document {ordinal}')
        values.append((raw + suffix.encode(), name))
    return values


class Workload:
    def __init__(self, directory, engine, variant, case, pdf, binding, python_command, producer_journal=None, *, database_provenance=None):
        from benchmarks.knowledge_html.durable import DurableHtmlCorpus
        self.root, self.engine = Path(directory), Path(engine)
        self.variant, self.case, self.pdf = variant, case, pdf
        self.binding_options, self.python_command = binding, python_command
        self.service = None
        self.calls = 0
        self.closed = False
        self.profile = None
        self.producer_receipts = []
        self.producer_journal = producer_journal
        self.database_provenance = database_provenance
        self.corpus = None
        if case != 'cancellation':
            self.corpus = DurableHtmlCorpus(self.root, self.engine, variant='candidate',
                owner_user_id=900071, html_projector=self.project if variant=='candidate' else None,
                database_provenance=database_provenance)
            self.db_identity = self.corpus.build_identity
        else: self.db_identity = None

    def project(self, value):
        if self.service is None:
            from lib.knowledge.native_html_service import LazyHtmlProjector
            self.service = LazyHtmlProjector(lambda: create_binding(self.binding_options))
        result = self.service.project(value)
        self.calls += 1
        self.profile = self.service.profile
        return result

    def inputs(self, index):
        return fixture_inputs(self.case, self.pdf, index)

    def operation(self, index):
        if self.case == 'cancellation':
            from benchmarks.knowledge_html.native_ingest_interrupt import interrupt
            result = interrupt(self.engine, self.variant, self.pdf, index, self.binding_options,
                               self.python_command,memory_observation=self.producer_journal is not None,
                               database_provenance=self.database_provenance)
            receipt={key:result[key] for key in
                ('producer_pid','producer_identity','producer_returncode','projection_calls','html_profile','producer_child_memory_receipts')}
            if self.producer_journal is None:self.producer_receipts.append(receipt)
            else:self.producer_journal.append(receipt)
            self.calls += result['projection_calls']
            self.profile = result['html_profile']
            self.db_identity = result['db_identity']
            return [result['page']]
        pages = []
        for raw,name in self.inputs(index):
            result = self.corpus.add_document(raw,name,user_id=900071)
            assert not result['duplicate']
            page = self.corpus.get_document_content(result['id'],user_id=900071,limit=200)
            assert page is not None and not page['pagination']['has_more']
            assert len(page['chunks']) == result['chunk_count']
            pages.append(page)
        return pages

    def verify(self, index, pages):
        from benchmarks.knowledge_html.durable import semantic_projection
        if self.case != 'cancellation':
            for raw,_ in self.inputs(index):
                paths=list((self.root/'sources'/'900071'/'sources').glob(hashlib.sha256(raw).hexdigest()+'*'))
                assert len(paths)==1 and paths[0].read_bytes()==raw
        return digest([semantic_projection(page) if 'id' in page['document'] else page for page in pages])

    def retire(self):
        # Release warmup owners before the cohort baseline in both modes.
        failures=[]
        try:
            if self.corpus is not None:self.corpus.close()
        except BaseException as error:failures.append(error)
        try:
            if self.service is not None:self.service.close();self.service=None
        except BaseException as error:failures.append(error)
        if failures:raise BaseExceptionGroup('owned ingestion did not retire',failures)

    def reopen(self):
        if self.corpus is not None: self.corpus.open()

    def close(self):
        if not self.closed:
            self.retire()
            self.closed = True


def measure(config):
    from benchmarks.process_memory_counters import process_status
    from benchmarks.repository_index.record_journal import RecordJournal
    from benchmarks.repository_index.child_memory import observe_children
    instrumentation = instrumentation_state()
    mode, samples = config['mode'], config['samples']
    assert mode in ('warm','cold','memory') and samples>0
    identity = process_identity()
    pdf = Path(config['pdf']).read_bytes()
    memory = mode=='memory'
    with (observe_children() if memory else nullcontext()) as children, (
            RecordJournal() if memory else nullcontext()) as journal, (
            RecordJournal() if memory else nullcontext()) as producer_journal, (
            tempfile.TemporaryDirectory(prefix='tofu-native-html-work-')) as directory:
        # Manifest parsing can spawn children. It is outside clocks but must
        # remain inside original-wait capture, just like every observed child.
        if 'database_provenance' in config:
            provenance = DatabaseProvenance.from_receipt(config['database_provenance'])
            provenance.verify(ROOT, config['engine'])
        else:
            provenance = DatabaseProvenance.capture(ROOT, config['engine'])
        work = Workload(directory,config['engine'],config['variant'],config['case'],pdf,
                        config['binding'],config['python_command'],producer_journal,
                        database_provenance=provenance)
        try:
            result, first = clock(work.operation,-1)
            first_output = work.verify(-1,result); del result
            raw, trace = [], hashlib.sha256()
            if memory: journal.append(first)
            baseline = None
            if mode == 'cold':
                raw = [first]; output_trace = first_output
            else:
                for index in range(20):
                    result = work.operation(index); work.verify(index,result); del result
                work.retire()
                if memory:
                    gc.collect(); baseline = process_status(os.getpid())['VmRSS']
                work.reopen()
                for index in range(20,20+samples):
                    result, clocks = clock(work.operation,index)
                    trace.update(bytes.fromhex(work.verify(index,result)))
                    if journal is None: raw.append(clocks)
                    else: journal.append(clocks)
                    del result,clocks
                output_trace = trace.hexdigest()
            work.close()
            provenance.verify(ROOT, config['engine'])
            if memory: gc.collect()
            counters = process_status(os.getpid()) if memory else None
            # No cohort-sized clock or child list is materialized before counters.
            journal_receipt = None
            if memory:
                rows = journal.read(); assert rows[0]==first and len(rows)==samples+1
                raw=rows[1:];journal_receipt=journal.receipt()
            profile, calls, db_identity = work.profile, work.calls, work.db_identity
            producer_receipts = producer_journal.read() if memory else work.producer_receipts
        finally:
            primary = sys.exception()
            try: work.close()
            except BaseException as cleanup:
                if primary is None: raise
                primary.add_note('native ingest cleanup failed: '+repr(cleanup))
    walls=[r['wall_ns']/1e6 for r in raw];cpus=[r['cpu_ns']/1e6 for r in raw];ordered=sorted(walls)
    after = instrumentation_state()
    return dict(variant=config['variant'],case=config['case'],mode=mode,process_identity=identity,
        pid=os.getpid(),python=sys.version,samples=len(raw),warmup_operations=0 if mode=='cold' else 20,
        clock_scope=CLOCK_SCOPE,database_provenance=provenance.receipt(),
        wall_samples_ms=walls,cpu_samples_ms=cpus,raw_clocks=raw,cpu_scope=CPU_SCOPE,
        first_operation=dict(first,output_sha256=first_output),output_trace_sha256=output_trace,
        p50_ms=statistics.median(walls),p95_ms=ordered[int((len(walls)-1)*.95)],
        p99_ms=ordered[int((len(walls)-1)*.99)],cpu_ms_per_operation=statistics.mean(cpus),
        throughput_per_second=len(walls)*1000/sum(walls),projection_calls=calls,html_profile=profile,
        db_identity=db_identity,joined_cleanup=work.closed,producer_receipts=producer_receipts,
        memory_separate_process=memory,process_image_counters=counters,
        retention_baseline_rss_bytes=baseline,post_cleanup_rss_bytes=counters['VmRSS'] if memory else None,
        retained_bytes=max(0,counters['VmRSS']-baseline) if memory else None,
        parent_peak_rss_bytes=counters['VmHWM'] if memory else None,
        child_memory_receipts=children.rows if memory else None,
        clock_record_journal=journal_receipt,clock_records_materialized_after_memory_counters=memory,
        sealed_html_image_bytes=config['binding']['bytes'] if profile else 0,
        profiler_enabled=instrumentation['profiler_enabled'] or after['profiler_enabled'],
        tracemalloc_enabled=instrumentation['tracemalloc_enabled'] or after['tracemalloc_enabled'],
        affinity=sorted(os.sched_getaffinity(0)))
