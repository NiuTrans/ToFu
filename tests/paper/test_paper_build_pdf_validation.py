"""Real parser rejection, worker cancellation and native resource bounds."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import zlib

import psutil
import pytest

from lib.paper.builds import pdf_validation
from lib.paper.builds.budget import validation_memory_bytes
from lib.paper.builds.contract import limit
from lib.paper.builds.errors import BuildError
from tests.support.paper_pdf import paper_objects, pdf_from_objects, paper_pdf

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _file(tmp_path, data):
    path = tmp_path / 'paper.pdf'
    path.write_bytes(data)
    return path


def test_real_pdf_uses_a_bounded_worker_and_reclaims_its_report(tmp_path):
    result = pdf_validation.validate_pdf(_file(tmp_path, paper_pdf()))
    assert result['valid'] and result['pages'] == 1 and result['objects'] >= 5
    assert 0 < result['observed_memory_bytes'] <= validation_memory_bytes()
    assert not list(tmp_path.glob('.pdf-validation-*'))


@pytest.mark.parametrize('corruption', ['empty_body', 'xref', 'missing_object', 'page_count',
                                       'page_cycle', 'parent', 'xref_size', 'geometry', 'flate', 'content'])
def test_header_and_eof_cannot_hide_invalid_pdf_structure(tmp_path, corruption):
    objects = paper_objects()
    if corruption == 'empty_body':
        data = b'%PDF-1.7\nnot a PDF body\n%%EOF\n'
    else:
        if corruption == 'missing_object':
            objects[2] = objects[2].replace(b'/Contents 4 0 R', b'/Contents 9 0 R')
        elif corruption == 'page_count':
            objects[1] = objects[1].replace(b'/Count 1', b'/Count 2')
        elif corruption == 'page_cycle':
            objects[1] = objects[1].replace(b'[3 0 R]', b'[2 0 R]')
        elif corruption == 'parent':
            objects[2] = objects[2].replace(b'/Parent 2 0 R', b'/Parent 1 0 R')
        elif corruption == 'geometry':
            objects[2] = objects[2].replace(b'[0 0 300 400]', b'[0 0 -300 400]')
        elif corruption == 'flate':
            objects[3] = b'<< /Length 3 /Filter /FlateDecode >>\nstream\nbad\nendstream'
        elif corruption == 'content':
            content = b'BT (unterminated'
            objects[3] = b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream'
        data = pdf_from_objects(objects)
        if corruption == 'xref':
            start = data.rsplit(b'startxref\n', 1)
            data = start[0] + b'startxref\n0\n%%EOF\n'
        elif corruption == 'xref_size':
            data = data.replace(b'/Size 6', b'/Size 5')
    with pytest.raises(BuildError) as error:
        pdf_validation.validate_pdf(_file(tmp_path, data))
    assert error.value.code == 'pdf_invalid'
    assert not list(tmp_path.glob('.pdf-validation-*'))


def test_inert_unreferenced_streams_are_audited_and_decompression_is_bounded(tmp_path):
    objects = paper_objects()
    compressed = zlib.compress(b'x' * (limit('pdf_validation_stream_bytes') + 1))
    objects.append(b'<< /Filter /FlateDecode /Length '+str(len(compressed)).encode()+
                   b' >>\nstream\n'+compressed+b'\nendstream')
    with pytest.raises(BuildError) as error:
        pdf_validation.validate_pdf(_file(tmp_path, pdf_from_objects(objects)))
    assert error.value.code == 'pdf_limit'


def test_native_worker_memory_limit_returns_a_bounded_failure_report(tmp_path):
    objects = paper_objects()
    compressed = zlib.compress(b'x' * (20 * 1024 * 1024))
    stream = b'<< /Filter /FlateDecode /Length '+str(len(compressed)).encode()+b' >>\nstream\n'+compressed+b'\nendstream'
    objects.extend([stream] * 7)
    source = _file(tmp_path, pdf_from_objects(objects))
    report = tmp_path / 'report.json'
    memory = 128 * 1024 * 1024
    result = subprocess.run(pdf_validation._command(source, report, memory), cwd=ROOT,
                            capture_output=True, timeout=20)
    assert result.returncode == 3, result.stderr.decode(errors='replace')
    data = json.loads(report.read_text())
    assert data['error'] == 'pdf_limit' and not data['valid']
    assert 0 < data['observed_memory_bytes'] <= memory
    assert report.stat().st_size <= limit('pdf_validation_report_bytes')


def test_encrypted_and_empty_page_documents_are_rejected(tmp_path):
    from pypdf import PdfWriter
    path = tmp_path / 'paper.pdf'
    for encrypted in (False, True):
        writer = PdfWriter()
        if encrypted:
            writer.add_blank_page(width=300, height=400)
            writer.encrypt('fixture-password')
        writer.write(path)
        with pytest.raises(BuildError) as error:
            pdf_validation.validate_pdf(path)
        assert error.value.code == 'pdf_invalid'


@pytest.mark.parametrize('cancelled', [False, True])
def test_timeout_or_cancellation_kills_and_reaps_the_owned_child(tmp_path, monkeypatch, cancelled):
    path = _file(tmp_path, paper_pdf(0))
    pid_path = tmp_path / 'pid'
    command = [sys.executable, '-c',
        'import os,sys,time;from pathlib import Path;Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(60)',
        str(pid_path)]
    monkeypatch.setattr(pdf_validation, '_command', lambda *args: command)
    cancel = threading.Event()
    processes = []
    launch = pdf_validation.subprocess.Popen
    def launched(*args, **kwargs):
        process = launch(*args, **kwargs)
        processes.append(process)
        if cancelled:
            cancel.set()  # Cancellation is injected only after child creation.
        return process
    monkeypatch.setattr(pdf_validation.subprocess, 'Popen', launched)
    with pytest.raises(BuildError) as error:
        pdf_validation.validate_pdf(path, cancel=cancel, timeout=2 if cancelled else .3)
    assert error.value.code == ('cancelled' if cancelled else 'pdf_limit')
    assert len(processes) == 1 and processes[0].poll() is not None
    assert not psutil.pid_exists(processes[0].pid)
    assert not list(tmp_path.glob('.pdf-validation-*'))


@pytest.mark.parametrize('launcher', ['desktop/launcher.py', 'desktop/agent_launcher.py'])
def test_frozen_worker_dispatch_precedes_server_tray_and_data_startup(tmp_path, launcher):
    source = _file(tmp_path, paper_pdf(0))
    report = tmp_path / 'result.json'
    command = [sys.executable, '-c', '''
import runpy,sys
sys.path.insert(0,sys.argv[1]); launcher=sys.argv[2]
sys.frozen=True;sys.argv=[launcher,'--paper-validate-worker',*sys.argv[3:]]
sys.stdout=None;sys.stderr=None
runpy.run_path(launcher,run_name='__main__')
''', str(ROOT), str(ROOT / launcher), str(source), str(report), str(128 * 1024 * 1024)]
    result = subprocess.run(command, cwd=tmp_path, env={**os.environ, 'TOFU_RUN_SERVER': '1'},
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(report.read_text())['valid']
    assert not (tmp_path / 'data').exists()
