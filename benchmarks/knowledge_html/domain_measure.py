"""Native ingest five-case comparisons; original bytes and commits are included.

Cancellation unit is producer interruption after commit/before receipt, cold
recovery and digest retry. It does not invent a pre-commit cancellation protocol.
CPU includes this worker plus reaped producer/CLI children. Separate memory
workers sample aggregate owned-process RSS every 5ms and parent high-water RSS.
"""
import argparse
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
import statistics

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from benchmarks.knowledge_html.check_ingest_domain import documents,OWNER
from benchmarks.knowledge_html.durable import DurableHtmlCorpus,semantic_projection
from benchmarks.knowledge_html.interruption import interrupt_and_recover
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION
from runtime_guards import probe_system_resources,resource_budget_manifest
from benchmarks.system_performance import compare

CASES=('html','pdf','batch','cancellation','bounded_memory')


def cpu():
    children=resource.getrusage(resource.RUSAGE_CHILDREN)
    return time.process_time()+children.ru_utime+children.ru_stime


def rss():return int(Path('/proc/self/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')


class MemorySampler:
    def __init__(self):self.stop=threading.Event();self.peak=0;self.error=None;self.thread=threading.Thread(target=self.run)
    def run(self):
        import psutil
        process=psutil.Process()
        try:
            while not self.stop.is_set():
                total=0
                for member in [process,*process.children(recursive=True)]:
                    try:total+=member.memory_info().rss
                    except psutil.NoSuchProcess:pass  # A joined child may exit between enumeration and sampling.
                self.peak=max(self.peak,total);self.stop.wait(.005)
        except Exception as error:self.error=error
    def __enter__(self):self.thread.start();return self
    def __exit__(self,exc_type,primary,traceback):
        self.stop.set();self.thread.join()
        if self.error is not None:
            if primary is None:raise self.error
            primary.add_note('owned memory sampler failed: '+str(self.error))


def stage(variant,case,engine,pdf,samples):
    with tempfile.TemporaryDirectory(prefix='tofu-ingest-domain-measure-') as directory:
        corpus=None;producer_pids=[]
        try:
            if case!='cancellation':corpus=DurableHtmlCorpus(Path(directory),engine,variant=variant,owner_user_id=OWNER)
            def operation(index):
                if case=='cancellation':
                    raw,name=documents('html',pdf)[0]
                    result=interrupt_and_recover(engine,variant,raw+f'<!--revision {index}-->'.encode(),name)
                    producer_pids.append(result['producer_pid']);return [result['page']]
                pages=[]
                for ordinal,(raw,name) in enumerate(documents(case,pdf)):
                    suffix=(f'<!--revision {index} document {ordinal}-->' if name.endswith('.html') else f'\n%revision {index} document {ordinal}')
                    raw+=suffix.encode()
                    result=corpus.add_document(raw,name,user_id=OWNER)
                    assert not result['duplicate']
                    page=corpus.get_document_content(result['id'],user_id=OWNER,limit=200)
                    assert page is not None and len(page['chunks'])==result['chunk_count']
                    pages.append(semantic_projection(page))
                assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
                return pages
            start=time.perf_counter_ns();operation(-1);cold=(time.perf_counter_ns()-start)/1e6
            for i in range(20):operation(i)
            raw=[];trace=hashlib.sha256()
            for i in range(20,20+samples):
                start=time.perf_counter_ns();before_cpu=cpu();value=operation(i)
                raw.append(((time.perf_counter_ns()-start)/1e6,(cpu()-before_cpu)*1000))
                trace.update(json.dumps(value,sort_keys=True).encode());trace.update(b'\0')
            walls=[r[0] for r in raw];ordered=sorted(walls)
            assert len(set(producer_pids))==len(producer_pids)
            return dict(process_identity=os.getpid(),wall_samples_ms=walls,cpu_samples_ms=[r[1] for r in raw],
                p50_ms=statistics.median(walls),p95_ms=ordered[int((samples-1)*.95)],p99_ms=ordered[int((samples-1)*.99)],
                throughput_per_second=samples*1000/sum(walls),cpu_ms_per_operation=statistics.mean(r[1] for r in raw),
                output_trace_sha256=trace.hexdigest(),interrupted_producer_pids=producer_pids,
                cold_first_operation_ms=cold,warmup_operations=20,profiler_enabled=False,tracemalloc_enabled=False,
                memory_separate_process=False,final_admission_unfinished=CLASSIC_PDF_ADMISSION.snapshot()['unfinished'])
        finally:
            if corpus is not None:
                primary=sys.exc_info()[1]
                try:corpus.close()
                except Exception as error:
                    if primary is None:raise
                    primary.add_note('owned ingest authority close failed: '+str(error))


def worker(variant,case,engine,pdf,samples,memory):
    if memory:
        stage(variant,case,engine,pdf,1);gc.collect();baseline=rss()
        with MemorySampler() as sampler:result=stage(variant,case,engine,pdf,samples)
        gc.collect();after=rss()
        result.update(peak_rss_bytes=max(sampler.peak,resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024),retained_bytes=max(0,after-baseline),retention_baseline_rss_bytes=baseline,post_cleanup_rss_bytes=after)
    else:result=stage(variant,case,engine,pdf,samples)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',choices=('reference','candidate'));parser.add_argument('--case',choices=CASES)
    parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--pdf',type=Path,required=True)
    parser.add_argument('--memory',action='store_true');parser.add_argument('--samples',type=int,default=100);parser.add_argument('--pairs',type=int,default=3);parser.add_argument('--output-prefix',type=Path)
    args=parser.parse_args();pdf=args.pdf.read_bytes()
    if args.worker:
        print('TOFU_INGEST_MEASUREMENT='+json.dumps(worker(args.worker,args.case,args.engine,pdf,args.samples,args.memory)));return
    assert args.output_prefix and args.samples>0 and args.pairs>0
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    checks={}
    for name,extra in [('check_ingest_domain.py',['--pdf',str(args.pdf)]),('check_interruption.py',[])]:
        result=subprocess.run([python,'-m','benchmarks.knowledge_html.'+name.removesuffix('.py'),'--engine',str(args.engine),*extra],cwd=ROOT,check=True,capture_output=True,text=True,timeout=300)
        checks[name]=result.stdout.strip()
    names=['benchmarks/knowledge_html/'+n for n in ('domain_measure.py','interruption.py','check_interruption.py','check_ingest_domain.py','durable.py','reference.py','fixture_pdf.py')]
    names+=['lib/knowledge/store.py','lib/knowledge/ingest.py','lib/knowledge/html_text.py','lib/knowledge/assets.py','lib/knowledge/repository.py','lib/knowledge/resource_policy.py','lib/pdf_parser/text.py','lib/pdf_parser/policy.py','lib/pdf_parser/admission.py','lib/storage/v2_embedded_client.py','serverctl_pkg/native_engine.py']
    names += [str(p.relative_to(ROOT)) for p in (ROOT/'tofu_db').glob('*.py')]
    sources={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
    library=hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest();engine_sha=hashlib.sha256(args.engine.read_bytes()).hexdigest();pdf_sha=hashlib.sha256(pdf).hexdigest()
    runtime=dict(python=sys.version,platform=platform.platform(),packages={n:importlib.metadata.version(n) for n in ('PyMuPDF','pymupdf4llm','beautifulsoup4','psutil')},native_library_sha256=library,native_engine_sha256=engine_sha)
    hardware=resource_budget_manifest(snapshot=probe_system_resources())
    reports={v:dict(format='tofu.system-performance/v1',scope=__doc__,sources=sources,check_receipts=checks,pdf_sha256=pdf_sha,workloads=[],
        limitations=['process interruption after commit; no pre-commit/task/HTTP cancellation protocol','RSS sampled at 5ms; short child peaks may be missed','CPU is parent plus reaped children; live resource-tracker CPU excluded','retention is one post-disposal RSS-growth interval, not long-run leak proof']) for v in ('reference','candidate')}
    seen=set()
    for case in CASES:
        fixture_hash=hashlib.sha256(json.dumps(dict(case=case,pdf=pdf_sha,samples=args.samples,html='64-row table;large=8copies x3documents',interruption='commit barrier->kill/join->cold recovery->digest retry'),sort_keys=True).encode()).hexdigest();rows={}
        for variant in reports:
            identity=hashlib.sha256(json.dumps(dict(sources=sources,variant=variant,runtime=runtime),sort_keys=True).encode()).hexdigest()
            row=dict(domain='ingest',case=case,scope='domain_integration',correctness_passed=True,failure_and_cleanup_passed=True,
                identity=dict(source_sha256=identity,runtime=runtime,hardware=hardware,fixture_sha256=fixture_hash),measurements=[])
            rows[variant]=row;reports[variant]['workloads'].append(row)
        for pair in range(args.pairs):
            values={}
            for variant in (('reference','candidate') if pair%2==0 else ('candidate','reference')):
                assert sources=={n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}
                assert hashlib.sha256(args.pdf.read_bytes()).hexdigest()==pdf_sha
                assert hashlib.sha256(args.engine.read_bytes()).hexdigest()==engine_sha
                assert hashlib.sha256(args.engine.with_name('libtofu_db.so').read_bytes()).hexdigest()==library
                command=[python,__file__,'--worker',variant,'--case',case,'--engine',str(args.engine),'--pdf',str(args.pdf),'--samples',str(args.samples)]
                def invoke(extra):
                    output=subprocess.run(command+extra,check=True,capture_output=True,text=True,timeout=1800).stdout
                    lines=[line.removeprefix('TOFU_INGEST_MEASUREMENT=') for line in output.splitlines() if line.startswith('TOFU_INGEST_MEASUREMENT=')];assert len(lines)==1
                    result=json.loads(lines[0]);assert result['process_identity'] not in seen;seen.add(result['process_identity']);return result
                value=invoke([]);memory=invoke(['--memory'])
                assert value['output_trace_sha256']==memory['output_trace_sha256']
                value.update(peak_rss_bytes=memory['peak_rss_bytes'],retained_bytes=memory['retained_bytes'],memory_process_identity=memory['process_identity'],memory_separate_process=True,
                    retention_baseline_rss_bytes=memory['retention_baseline_rss_bytes'],post_cleanup_rss_bytes=memory['post_cleanup_rss_bytes'])
                values[variant]=value
            assert values['reference']['output_trace_sha256']==values['candidate']['output_trace_sha256']
            for variant in reports:
                rows[variant]['measurements'].append(values[variant]);Path(str(args.output_prefix)+'-'+variant+'.json').write_text(json.dumps(reports[variant],indent=2)+'\n')
            print(case,pair,'p50 ratio',round(values['candidate']['p50_ms']/values['reference']['p50_ms'],3),flush=True)
    verdict=compare(reports['reference'],reports['candidate']);Path(str(args.output_prefix)+'-verdict.json').write_text(json.dumps(verdict,indent=2)+'\n');print('whole-system verdict',verdict['verdict'],'coverage',verdict['coverage'],flush=True)


if __name__=='__main__':main()
