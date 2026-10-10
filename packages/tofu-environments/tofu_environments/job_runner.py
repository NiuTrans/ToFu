"""POSIX batch entrypoint: argv execution, wall deadline and bounded evidence.

Cluster templates invoke this module with a sealed JSON spec. The scheduler
owns CPU/memory/GPU allocation; this runner does not claim scheduler admission.
"""
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


def run_job(spec, workdir):
    root = Path(workdir).resolve(strict=True)
    seconds = spec['limits']['runtime_seconds']
    maximum = spec['limits']['result_bytes']
    if type(seconds) is not int or seconds <= 0 or type(maximum) is not int or maximum <= 0:
        raise ValueError('positive runtime and output bounds required')
    command = spec['command']
    if not isinstance(command,list) or not command or not all(isinstance(x,str) and x for x in command):
        raise ValueError('explicit argv required')
    start = time.monotonic()
    process = subprocess.Popen(command,cwd=root,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    selector = selectors.DefaultSelector()
    captured = {'stdout':bytearray(),'stderr':bytearray()}
    used = 0
    failure = None
    try:
        for name, stream in [('stdout',process.stdout),('stderr',process.stderr)]:
            os.set_blocking(stream.fileno(),False)
            selector.register(stream,selectors.EVENT_READ,name)
        while selector.get_map():
            remaining = seconds-(time.monotonic()-start)
            if remaining <= 0:
                failure='runtime_limit';break
            for key,_ in selector.select(min(remaining,0.1)):
                chunk=os.read(key.fileobj.fileno(),min(65536,maximum-used+1))
                if not chunk:
                    selector.unregister(key.fileobj);continue
                used+=len(chunk)
                if used > maximum:
                    failure='result_limit';break
                captured[key.data].extend(chunk)
            if failure:break
        if failure is None:
            try:process.wait(timeout=max(0.001,seconds-(time.monotonic()-start)))
            except subprocess.TimeoutExpired:failure='runtime_limit'
    finally:
        # The process group may outlive its leader; always reclaim descendants.
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        process.wait()
        selector.close()
        process.stdout.close();process.stderr.close()
    receipts=[]
    if failure is None and process.returncode==0:
        for name in spec['outputs']:
            parts=Path(name).parts
            if not parts or Path(name).is_absolute() or '..' in parts or '\\' in name:
                failure='invalid_output';break
            candidate=root
            for part in parts:
                candidate=candidate/part
                if candidate.is_symlink():failure='invalid_output';break
            if failure:break
            if not candidate.is_file():failure='missing_output';break
            digest=hashlib.sha256();size=0
            with candidate.open('rb') as stream:
                while True:
                    if time.monotonic()-start >= seconds:
                        failure='runtime_limit';break
                    chunk=stream.read(min(65536,maximum-used+1))
                    if not chunk:break
                    used+=len(chunk);size+=len(chunk)
                    if used>maximum:failure='result_limit';break
                    digest.update(chunk)
            if failure:break
            receipts.append({'name':name,'bytes':size,'sha256':digest.hexdigest()})
    return {'state':'succeeded' if failure is None and process.returncode==0 else 'failed',
            'exit_code':process.returncode,'error':failure,
            'elapsed_seconds':time.monotonic()-start,'outputs':receipts,
            'stdout':bytes(captured['stdout']).decode('utf-8',errors='replace'),
            'stderr':bytes(captured['stderr']).decode('utf-8',errors='replace')}


def main():
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('spec');parser.add_argument('--workdir',default='.')
    args=parser.parse_args()
    spec=json.loads(Path(args.spec).read_text())
    result=run_job(spec,args.workdir)
    print('TOFU_JOB_RESULT='+json.dumps(result,sort_keys=True),flush=True)
    raise SystemExit(0 if result['state']=='succeeded' else 1)

if __name__=='__main__':main()
