#!/usr/bin/env python3
"""Independent-process full knowledge HTML extraction experiment; no storage writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import statistics
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def inputs(case,index):
    article='<html><script>hidden</script><h1>Evidence</h1>'+''.join(
        '<p>Paragraph '+str(i)+': '+('reader 中文 &amp; evidence '*12)+'</p>' for i in range(32))+'</html>'
    table='<table>'+''.join('<tr><th>Field '+str(i)+'</th><td>82|181</td><td>&#128; 中文</td></tr>' for i in range(96))+'</table>'
    documents=[article] if case=='article' else [table] if case=='table' else [article]*8
    return [((body+'<!--revision '+str(index)+'-->').encode(),f'fixture-{i}.html') for i,body in enumerate(documents)]


def worker(args):
    initialized=time.perf_counter_ns()
    from lib.knowledge import ingest
    from benchmarks.knowledge_html.reference import load_reference
    reference,reference_sha256=load_reference()
    if args.variant=='reference':ingest._html_to_text=reference
    initialization_ms=(time.perf_counter_ns()-initialized)/1e6
    def run(index):return [ingest.extract(raw,name) for raw,name in inputs(args.case,index)]
    began=time.perf_counter_ns();cold=run(-1);cold_ms=(time.perf_counter_ns()-began)/1e6
    digest=hashlib.sha256(json.dumps(cold,sort_keys=True,ensure_ascii=True).encode())
    walls=[];cpus=[];last=None
    for index in range(120):
        documents=inputs(args.case,index)
        cpu_started=time.process_time_ns();started=time.perf_counter_ns()
        out=[ingest.extract(raw,name) for raw,name in documents]
        elapsed=(time.perf_counter_ns()-started)/1e6;cpu=(time.process_time_ns()-cpu_started)/1e6
        if index>=20:walls.append(elapsed);cpus.append(cpu)
        encoded=json.dumps(out,sort_keys=True,ensure_ascii=True).encode();digest.update(encoded);last=encoded
    ordered=sorted(walls)
    return {'reference_source_sha256':reference_sha256,'p50_ms':statistics.median(walls),'p95_ms':ordered[94],'p99_ms':ordered[98],
        'cpu_ms_per_operation':statistics.mean(cpus),'throughput_per_second':1000/statistics.mean(walls),
        'wall_samples_ms':walls,'cpu_samples_ms':cpus,'initialization_ms':initialization_ms,
        'cold_operation_ms':cold_ms,'peak_process_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
        'retained_output_bytes':len(last),'output_trace_sha256':digest.hexdigest(),'pid':os.getpid()}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker',action='store_true');parser.add_argument('--variant',choices=['reference','candidate'])
    parser.add_argument('--case',choices=['article','table','batch']);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.worker:print(json.dumps(worker(args)));return
    assert args.output is not None
    report={'scope':'full lib.knowledge.ingest.extract HTML text/metadata; synthetic fixtures, no repository commit/PDF/download/cancellation',
        'whole_system_verdict':'incomplete','production_promotion':False,'runtime':{'python':sys.version,'platform':platform.platform(),'affinity':sorted(os.sched_getaffinity(0))},
        'sources':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['benchmarks/knowledge_html/extractor.py','benchmarks/knowledge_html/check.py','benchmarks/knowledge_html/measure.py','lib/knowledge/ingest.py','lib/knowledge/html_text.py','benchmarks/knowledge_html/reference.py']},
        'memory_limit':'independent worker peak includes imports, fixtures, warmups and untimed output hashing','cases':[]}
    for case in ['article','table','batch']:
        pairs=[]
        for pair in range(3):
            measurements={}
            for variant in (['reference','candidate'] if pair%2==0 else ['candidate','reference']):
                def invoke():
                    result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--variant',variant,'--case',case],check=True,capture_output=True,text=True,timeout=120)
                    return json.loads(result.stdout)
                value=invoke();memory=invoke()
                assert value['pid']!=memory['pid'] and value['output_trace_sha256']==memory['output_trace_sha256']
                value['peak_process_rss_bytes']=memory['peak_process_rss_bytes'];value['memory_pid']=memory['pid']
                measurements[variant]=value
            assert measurements['reference']['output_trace_sha256']==measurements['candidate']['output_trace_sha256']
            pairs.append(measurements)
            print(case,pair,{k:round(measurements['candidate'][k]/measurements['reference'][k],3) for k in ['p50_ms','p99_ms','cpu_ms_per_operation','peak_process_rss_bytes']},flush=True)
        report['cases'].append({'case':case,'pairs':pairs})
        args.output.write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
