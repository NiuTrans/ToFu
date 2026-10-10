"""Actual native HTML/PDF/mixed ingest, source preservation and admission cleanup.

Uses frozen PDF bytes supplied by the caller. Checks before measurement; full
performance and ordinary HTTP cancellation remain separate evidence owners.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from benchmarks.knowledge_html.durable import DurableHtmlCorpus,semantic_projection
from lib.pdf_parser.admission import CLASSIC_PDF_ADMISSION

OWNER=900001


def documents(case,pdf):
    table=b'<h1>Source provenance</h1><table>'+b''.join(f'<tr><td>Row {i}</td><td>82|181 &amp; exact evidence</td></tr>'.encode() for i in range(64))+b'</table>'
    large=table*8
    if case=='html':return [(table,'fixture.html')]
    if case=='pdf':return [(pdf,'fixture.pdf')]
    if case=='batch':return [(table,'fixture.html'),(pdf,'fixture.pdf')]
    return [(large+f'<!--document {i}-->'.encode(),f'large-{i}.html') for i in range(3)]


def check(engine,pdf_path):
    pdf=pdf_path.read_bytes();traces={}
    for variant in ('reference','candidate'):
        pages=[]
        with tempfile.TemporaryDirectory(prefix='tofu-ingest-domain-check-') as directory:
            corpus=DurableHtmlCorpus(Path(directory),engine,variant=variant,owner_user_id=OWNER)
            try:
                for case in ('html','pdf','batch','bounded_memory'):
                    for raw,name in documents(case,pdf):
                        raw+=('<!--case '+case+'-->' if name.endswith('.html') else '\n%case '+case).encode()
                        result=corpus.add_document(raw,name,user_id=OWNER)
                        page=corpus.get_document_content(result['id'],user_id=OWNER,limit=200)
                        assert page is not None and len(page['chunks'])==result['chunk_count']
                        assert not page['pagination']['has_more']
                        source=list((corpus.root/'sources'/str(OWNER)/'sources').glob(hashlib.sha256(raw).hexdigest()+'*'))
                        assert len(source)==1 and source[0].read_bytes()==raw
                        pages.append(semantic_projection(page))
                        assert CLASSIC_PDF_ADMISSION.snapshot()['unfinished']==0
                corpus.close();corpus.open()
                assert len(corpus.repository(OWNER).documents())==7
            finally:corpus.close()
        traces[variant]=json.dumps(pages,sort_keys=True)
    assert traces['reference']==traces['candidate']
    print('PASS: HTML/PDF/mixed/large native upload/readback exact; seven original sources preserved; reopen; PDF unfinished admission0')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--engine',type=Path,required=True);parser.add_argument('--pdf',type=Path,required=True)
    args=parser.parse_args();check(args.engine,args.pdf)
