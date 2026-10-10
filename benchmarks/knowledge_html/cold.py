"""Fresh-process first full HTML extraction; common import and use timed separately."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def worker(args):
    started=time.perf_counter_ns();cpu_started=time.process_time_ns()
    from lib.knowledge import ingest
    import_ms=(time.perf_counter_ns()-started)/1e6;import_cpu_ms=(time.process_time_ns()-cpu_started)/1e6
    tree=ast.parse(Path(args.reference_source).read_text())
    function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_html_to_text')
    namespace=dict(ingest.__dict__);exec(compile(ast.Module(body=[function],type_ignores=[]),'<cold-reference>','exec'),namespace)
    if args.variant=='reference':ingest._html_to_text=namespace['_html_to_text']
    raw=('<h1>cold 中文</h1>'+('<p>evidence &amp; ownership</p>'*32)).encode()
    started=time.perf_counter_ns();cpu_started=time.process_time_ns();out=ingest.extract(raw,'cold.html')
    operation_ms=(time.perf_counter_ns()-started)/1e6;operation_cpu_ms=(time.process_time_ns()-cpu_started)/1e6
    return {'import_ms':import_ms,'import_cpu_ms':import_cpu_ms,'operation_ms':operation_ms,
        'operation_cpu_ms':operation_cpu_ms,'total_ms':import_ms+operation_ms,'total_cpu_ms':import_cpu_ms+operation_cpu_ms,
        'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024,
        'output_sha256':hashlib.sha256(json.dumps(out,sort_keys=True).encode()).hexdigest(),'pid':os.getpid()}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',action='store_true')
    parser.add_argument('--variant');parser.add_argument('--reference-source');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.worker:print(json.dumps(worker(args)));return
    report={'scope':'first full knowledge HTML extraction in fresh processes; source-reference compilation excluded',
        'whole_system_verdict':'incomplete','samples_per_variant':100,'pairs':[]}
    with tempfile.TemporaryDirectory(prefix='tofu-knowledge-cold-') as temporary:
        reference=Path(temporary)/'reference.py'
        reference.write_bytes(subprocess.check_output(['/usr/bin/git','show','8c62baae3:lib/knowledge/ingest.py'],cwd=ROOT))
        report['reference_sha256']=hashlib.sha256(reference.read_bytes()).hexdigest()
        for pair in range(3):
            values={}
            for variant in (['reference','candidate'] if pair%2==0 else ['candidate','reference']):
                rows=[]
                for _ in range(100):
                    run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker','--variant',variant,'--reference-source',str(reference)],capture_output=True,text=True,check=True,timeout=60)
                    rows.append(json.loads(run.stdout))
                values[variant]={'rows':rows,**{name:statistics.median(row[name] for row in rows) for name in ['total_ms','total_cpu_ms','peak_rss_bytes','operation_ms']}}
                print(pair,variant,{k:v for k,v in values[variant].items() if k!='rows'},flush=True)
            assert {row['output_sha256'] for v in values.values() for row in v['rows']}.__len__()==1
            report['pairs'].append(values);args.output.write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':main()
