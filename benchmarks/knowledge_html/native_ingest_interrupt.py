"""Owned HTML producer SIGKILL after durable commit, joined recovery and retry.

This is receipt interruption, not an invented pre-commit HTTP cancel protocol.
Only the spawned producer is killed; original authority/source bytes survive.
"""
import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def producer(config):
    from benchmarks.knowledge_html.native_ingest_provenance import DatabaseProvenance
    from benchmarks.knowledge_html.native_ingest_worker import Workload, process_identity
    observation=nullcontext()
    if config.get('memory_observation'):
        from benchmarks.repository_index.child_memory import observe_children
        observation=observe_children()
    work=None
    try:
        with observation as children:
            work=Workload(config['directory'],config['engine'],config['variant'],'html',b'',
                          config['binding'],config['python_command'],
                          database_provenance=(DatabaseProvenance.from_receipt(config['database_provenance'])
                                               if config.get('database_provenance') else None))
            pages=work.operation(config['index'])
            work.verify(config['index'],pages)
        print(json.dumps(dict(page=pages[0],process_identity=process_identity(),projection_calls=work.calls,html_profile=work.profile,
                              child_memory_receipts=children.rows if children is not None else None,
                              db_identity=work.db_identity)),flush=True)
        sys.stdin.buffer.read(1)
    finally:
        if work is not None:work.close()


def receive(process, seconds=120):
    deadline=time.monotonic()+seconds; buffer=bytearray()
    os.set_blocking(process.stdout.fileno(),False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout,selectors.EVENT_READ)
        while b'\n' not in buffer:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not selector.select(remaining):raise TimeoutError('producer commit barrier timed out')
            raw=os.read(process.stdout.fileno(),65536)
            if not raw:raise RuntimeError('producer exited before durable barrier')
            buffer.extend(raw)
            if len(buffer)>8<<20:raise RuntimeError('producer barrier exceeds fixture bound')
    line,tail=bytes(buffer).split(b'\n',1)
    if tail:raise RuntimeError('producer published extra output')
    return json.loads(line)


def interrupt(engine,variant,pdf,index,binding,python_command,*,memory_observation=False,database_provenance=None):
    from benchmarks.knowledge_html.durable import DurableHtmlCorpus,semantic_projection
    from benchmarks.knowledge_html.native_ingest_worker import fixture_inputs
    with tempfile.TemporaryDirectory(prefix='tofu-native-html-interrupt-') as directory:
        root=Path(directory);request=root/'producer.json'
        config=dict(directory=directory,engine=str(engine),variant=variant,index=index,
                    memory_observation=memory_observation,
                    binding=binding,python_command=python_command,
                    database_provenance=database_provenance.receipt() if database_provenance else None)
        request.write_text(json.dumps(config))
        with tempfile.TemporaryFile() as errors:
            process=subprocess.Popen([*python_command,__file__,str(request)],cwd=ROOT,
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors)
            try:
                receipt=receive(process)
                assert process.poll() is None
                process.kill();process.communicate(timeout=30)
                assert process.returncode == -9
            except BaseException as primary:
                if process.poll() is None:process.kill()
                process.communicate(timeout=30)
                errors.seek(0);primary.add_note('producer stderr: '+errors.read().decode(errors='replace')[-2000:])
                raise
        corpus=DurableHtmlCorpus(root,Path(engine),variant='candidate',owner_user_id=900071,initialize=False,
                                 database_provenance=database_provenance)
        try:
            assert corpus.build_identity==receipt['db_identity']
            page=corpus.get_document_content(receipt['page']['document']['id'],user_id=900071,limit=200)
            assert page==receipt['page']
            # Reconstruct only fixture bytes; no second native parser is opened.
            raw,name=fixture_inputs('html',pdf,index)[0]
            paths=list((root/'sources'/'900071'/'sources').glob(hashlib.sha256(raw).hexdigest()+'*'))
            assert len(paths)==1 and paths[0].read_bytes()==raw
            prior=corpus.repository(900071).documents();assert len(prior)==1
            result=corpus.add_document(raw,name,user_id=900071)
            assert result['duplicate'] and result['id']==page['document']['id']
            assert corpus.repository(900071).documents()==prior
            return dict(page=semantic_projection(page),producer_pid=process.pid,
                        producer_identity=receipt['process_identity'],
                        producer_child_memory_receipts=receipt['child_memory_receipts'],
                        producer_returncode=process.returncode,**{key:receipt[key] for key in
                        ('projection_calls','html_profile','db_identity')})
        finally:corpus.close()


if __name__=='__main__':producer(json.loads(Path(sys.argv[1]).read_text()))
