"""Real owned-process interruption after commit, before upload receipt delivery.

Synchronous knowledge ingestion has no independent pre-commit cancel protocol.
This fixture kills only its own spawned producer after receiving an internal
commit barrier, joins it, cold-recovers the same native authority and retries
by digest. Durable documents/sources are preserved, never cleaned as transport.
"""
import hashlib
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from benchmarks.knowledge_html.durable import DurableHtmlCorpus,semantic_projection

OWNER=900001


def producer(root,engine,variant,raw,name,connection):
    corpus=DurableHtmlCorpus(Path(root),Path(engine),variant=variant,owner_user_id=OWNER)
    try:
        result=corpus.add_document(raw,name,user_id=OWNER)
        assert not result['duplicate']
        page=corpus.get_document_content(result['id'],user_id=OWNER,limit=200)
        connection.send((result['id'],semantic_projection(page)))
        # Internal commit barrier is not a delivered HTTP/client receipt.
        connection.recv()
    finally:
        corpus.close();connection.close()


def interrupt_and_recover(engine,variant,raw,name):
    python=os.environ.get('TOFU_BENCHMARK_PYTHON',sys.executable)
    multiprocessing.set_executable(python)
    context=multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory(prefix='tofu-ingest-interruption-') as directory:
        receiver,sender=context.Pipe(duplex=True)
        process=context.Process(target=producer,args=(directory,str(engine),variant,raw,name,sender),name='owned-ingest-producer')
        corpus=None
        try:
            process.start();sender.close()
            if not receiver.poll(120):raise RuntimeError('owned ingest producer did not reach commit barrier')
            document_id,expected=receiver.recv()
            assert process.is_alive()
            producer_pid=process.pid
            process.kill();process.join(timeout=30)
            assert not process.is_alive() and process.exitcode==-9
            corpus=DurableHtmlCorpus(Path(directory),Path(engine),variant=variant,owner_user_id=OWNER,initialize=False)
            actual=semantic_projection(corpus.get_document_content(document_id,user_id=OWNER,limit=200))
            assert actual==expected
            sources=list((corpus.root/'sources'/str(OWNER)/'sources').glob(hashlib.sha256(raw).hexdigest()+'*'))
            assert len(sources)==1 and sources[0].read_bytes()==raw
            documents=corpus.repository(OWNER).documents();assert len(documents)==1
            retry=corpus.add_document(raw,name,user_id=OWNER)
            assert retry['duplicate'] and retry['id']==document_id
            assert corpus.repository(OWNER).documents()==documents
            return dict(page=actual,duplicate_retry=True,producer_pid=producer_pid,producer_exitcode=process.exitcode,
                        native_build_identity=corpus.build_identity,library_sha256=corpus.library_sha256)
        finally:
            primary=sys.exc_info()[1]
            if process.pid is not None and process.is_alive():process.kill();process.join()
            receiver.close();sender.close()
            if corpus is not None:
                try:corpus.close()
                except Exception as error:
                    if primary is None:raise
                    primary.add_note('owned recovery authority close failed: '+str(error))
            if process.pid is not None:assert not process.is_alive()
