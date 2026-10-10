"""Web-only use never activates optional PDF/OCR native capabilities."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def run_isolated(source):
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, '-c', source], env=environment,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_web_import_and_metadata_do_not_probe_pdf_ocr():
    run_isolated(r'''
import importlib.abc,sys
attempts=[]
class ForbiddenPdf(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'pymupdf','pymupdf4llm','onnxruntime','rapidocr'}:
            attempts.append(fullname)
            raise AssertionError('Web-only use attempted optional PDF/OCR import')
sys.meta_path.insert(0,ForbiddenPdf())
import tofu_search
from tofu_search.fetch.html_extract import extract_html_publish_date
assert extract_html_publish_date('<meta name="date" content="2024-01-01">')=='2024-01-01'
assert not attempts
''')


def test_missing_pdf_dependency_remains_an_explicit_capability_failure():
    run_isolated(r'''
import importlib.abc,sys
attempts=[]
class MissingPdf(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'pymupdf','pymupdf4llm'}:
            attempts.append(fullname)
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0,MissingPdf())
from tofu_search.fetch.pdf_extract import extract_pdf_text
import tofu_search.fetch.pdf_extract as pdf
assert not attempts
assert extract_pdf_text(b'synthetic')=='[PDF extraction unavailable: pymupdf not installed]'
initial=list(attempts)
assert pdf.HAS_PYMUPDF is False
assert extract_pdf_text(b'synthetic')=='[PDF extraction unavailable: pymupdf not installed]'
assert attempts==initial
''')


def test_concurrent_first_use_publishes_one_complete_dependency_pair():
    run_isolated(r'''
import importlib.abc,importlib.util,sys,threading,time
from concurrent.futures import ThreadPoolExecutor
attempts=[]
class FakePdf(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'pymupdf','pymupdf4llm'}:
            return importlib.util.spec_from_loader(fullname,self)
    def create_module(self,spec):
        return None
    def exec_module(self,module):
        attempts.append(module.__name__)
        time.sleep(.02)
        module.ready=True
sys.meta_path.insert(0,FakePdf())
import tofu_search.fetch.pdf_extract as pdf
import tofu_search.fetch.utils as utils
barrier=threading.Barrier(16)
def query(index):
    barrier.wait(timeout=5)
    if index%2:
        assert utils.HAS_FITZ and utils.pymupdf.ready
    else:
        assert pdf.HAS_PYMUPDF and pdf.HAS_PYMUPDF4LLM
        assert pdf.pymupdf.ready and pdf.pymupdf4llm.ready
with ThreadPoolExecutor(max_workers=16) as pool:
    list(pool.map(query,range(16)))
assert attempts==['pymupdf','pymupdf4llm'],attempts
assert utils.pymupdf is pdf.pymupdf
''')


@pytest.mark.parametrize('rich_available', [True, False])
def test_first_pdf_activation_extracts_real_document_and_preserves_raw_fallback(rich_available):
    pymupdf = pytest.importorskip('pymupdf')
    document = pymupdf.open()
    try:
        page = document.new_page()
        page.insert_text((72, 72), 'Cold PDF activation marker')
        payload = document.tobytes()
    finally:
        document.close()
    run_isolated('PAYLOAD=' + repr(payload) + '\nRICH=' + repr(rich_available) + r'''
import importlib.abc,sys
if not RICH:
    class MissingRich(importlib.abc.MetaPathFinder):
        def find_spec(self,fullname,path=None,target=None):
            if fullname.split('.')[0]=='pymupdf4llm':
                raise ModuleNotFoundError(fullname)
    sys.meta_path.insert(0,MissingRich())
from tofu_search.fetch.pdf_extract import extract_pdf_text
assert 'pymupdf' not in sys.modules
first=extract_pdf_text(PAYLOAD)
assert 'Cold PDF activation marker' in first,first
assert 'pymupdf' in sys.modules
assert extract_pdf_text(PAYLOAD)==first
limited=extract_pdf_text(PAYLOAD,max_chars=8)
assert '[...truncated at ' in limited
if RICH:
    assert '[...truncated at 8 chars, page 1/1]' in limited
else:
    assert 'Cold PDF activation marker' in limited
''')
