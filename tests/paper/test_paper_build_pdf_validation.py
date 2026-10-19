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


def test_real_pdf_uses_owned_worker_and_reclaims_its_report(tmp_path,monkeypatch):
    monkeypatch.delenv("TOFU_PAPER_BUILD_VALIDATION_MAX_MIB",raising=False)
    result = pdf_validation.validate_pdf(_file(tmp_path, paper_pdf()))
    assert result['valid'] and result['pages'] == 1 and result['objects'] >= 5
    assert result['observed_memory_bytes'] > 0 and validation_memory_bytes()==0
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


def test_inert_unreferenced_streams_are_audited_without_old_decoding_quota(tmp_path):
    objects = paper_objects()
    compressed = zlib.compress(b'x' * (33 * 1024 * 1024))
    objects.append(b'<< /Filter /FlateDecode /Length '+str(len(compressed)).encode()+
                   b' >>\nstream\n'+compressed+b'\nendstream')
    result=pdf_validation.validate_pdf(_file(tmp_path, pdf_from_objects(objects)))
    assert result['valid'] and result['decoded_bytes']>33*1024*1024


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


def test_complete_page_tree_above_old_reader_quota_is_valid(tmp_path):
    count=1001
    objects=paper_objects()
    page_ids=[3]+list(range(6,6+count-1))
    objects[1]=b'<< /Type /Pages /Count '+str(count).encode()+b' /Kids ['+b' '.join(
        str(i).encode()+b' 0 R' for i in page_ids)+b'] >>'
    objects.extend([objects[2]]*(count-1))
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(objects)))
    assert result['valid'] and result['pages']==count


def test_complete_xref_above_old_object_quota_is_audited(tmp_path,monkeypatch):
    monkeypatch.setenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB','512')
    objects=paper_objects()+[b'null']*100001
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(objects)))
    assert result['valid'] and result['objects']>=100006


def test_complete_content_above_old_operation_quota_is_parsed(tmp_path,monkeypatch):
    monkeypatch.setenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB','512')
    objects=paper_objects();content=b'q Q\n'*125001
    objects[3]=b'<< /Length '+str(len(content)).encode()+b' >>\nstream\n'+content+b'\nendstream'
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(objects)))
    assert result['valid'] and result['decoded_bytes']==len(content)


def test_complete_stream_set_above_old_total_decoding_quota_is_valid(tmp_path,monkeypatch):
    monkeypatch.setenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB','512')
    raw_bytes=15*1024*1024;encoded=zlib.compress(b'x'*raw_bytes)
    stream=b'<< /Filter /FlateDecode /Length '+str(len(encoded)).encode()+b' >>\nstream\n'+encoded+b'\nendstream'
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(paper_objects()+[stream]*9)))
    assert result['valid'] and result['decoded_bytes']>128*1024*1024


@pytest.mark.parametrize('change',['truncated','trailing'])
def test_complete_flate_integrity_still_rejects_invalid_stream(tmp_path,change):
    encoded=zlib.compress(b'valid source body')
    encoded=encoded[:-1] if change=='truncated' else encoded+b'trailing bytes'
    stream=b'<< /Filter /FlateDecode /Length '+str(len(encoded)).encode()+b' >>\nstream\n'+encoded+b'\nendstream'
    with pytest.raises(BuildError) as error:
        pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(paper_objects()+[stream])))
    assert error.value.code=='pdf_invalid'


def test_in_process_structure_audit_restores_library_filter_policy(tmp_path):
    from pypdf import filters,get_configuration
    from lib.paper.builds.pdf_structure import inspect_pdf
    before=(filters.decompress,get_configuration())
    assert inspect_pdf(_file(tmp_path,paper_pdf()))['pages']==1
    assert (filters.decompress,get_configuration())==before


def test_library_declared_stream_extent_does_not_reintroduce_private_quota(tmp_path,monkeypatch):
    monkeypatch.setenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB','512')
    raw=b'x'*(76*1024*1024)
    stream=b'<< /Length '+str(len(raw)).encode()+b' >>\nstream\n'+raw+b'\nendstream'
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(paper_objects()+[stream])))
    assert result['valid'] and result['decoded_bytes']>76*1024*1024


def test_failed_in_process_audit_restores_library_configuration(tmp_path):
    from pypdf import filters,get_configuration
    from lib.paper.builds.pdf_structure import inspect_pdf,PdfValidationFailure
    before=(filters.decompress,get_configuration())
    objects=paper_objects();objects[1]=objects[1].replace(b'/Count 1',b'/Count 2')
    with pytest.raises(PdfValidationFailure):inspect_pdf(_file(tmp_path,pdf_from_objects(objects)))
    assert (filters.decompress,get_configuration())==before


def test_validator_default_accepts_real_decoded_demand_above_old_memory_ceiling(tmp_path,monkeypatch):
    monkeypatch.delenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB',raising=False)
    raw_bytes=60*1024*1024;encoded=zlib.compress(b'x'*raw_bytes)
    stream=b'<< /Filter /FlateDecode /Length '+str(len(encoded)).encode()+b' >>\nstream\n'+encoded+b'\nendstream'
    result=pdf_validation.validate_pdf(_file(tmp_path,pdf_from_objects(paper_objects()+[stream]*9)))
    assert result['valid'] and result['decoded_bytes']>540*1024*1024
    assert result['observed_memory_bytes']>512*1024*1024


def test_default_validator_can_finish_after_old_wall_deadline(tmp_path,monkeypatch):
    path=_file(tmp_path,paper_pdf())
    def command(source,report,memory,seconds):
        assert memory==0 and seconds==0
        return [sys.executable,'-c',
            'import time,json,sys;from pathlib import Path;time.sleep(15.2);'
            'Path(sys.argv[1]).write_text(json.dumps(dict(valid=True,error=None,pages=1,objects=5,decoded_bytes=1,observed_memory_bytes=0)))',str(report)]
    monkeypatch.delenv('TOFU_PAPER_BUILD_VALIDATION_MAX_MIB',raising=False)
    monkeypatch.setattr(pdf_validation,'_command',command)
    assert pdf_validation.validate_pdf(path)['valid']


@pytest.mark.parametrize('value',[0,1,768,2048])
def test_validator_explicit_memory_values_are_exact(value):
    assert validation_memory_bytes({'TOFU_PAPER_BUILD_VALIDATION_MAX_MIB':str(value)})==value*1024*1024


@pytest.mark.parametrize('value',['bad','-1'])
def test_invalid_validator_memory_override_refuses(value):
    with pytest.raises(ValueError):validation_memory_bytes({'TOFU_PAPER_BUILD_VALIDATION_MAX_MIB':value})


def test_explicit_validator_deadline_above_old_ceiling_reaches_worker(tmp_path,monkeypatch):
    path=_file(tmp_path,paper_pdf());original=pdf_validation._command;observed=[]
    def command(source,report,memory,seconds):
        observed.append(seconds);return original(source,report,memory,seconds)
    monkeypatch.setattr(pdf_validation,'_command',command)
    assert pdf_validation.validate_pdf(path,timeout=42)['valid']
    assert observed==[42]


@pytest.mark.skipif(sys.platform!='linux',reason='Linux native parent-death ownership')
def test_unlimited_validator_worker_retires_when_parent_dies(tmp_path):
    import time
    pid_file=tmp_path/'child.pid'
    child_code="import sys,os,time;from pathlib import Path;from lib.paper.builds.pdf_validation_limits import bind_parent_lifetime;bind_parent_lifetime(int(sys.argv[1]));Path(sys.argv[2]).write_text(str(os.getpid()));time.sleep(60)"
    parent_code="import subprocess,sys,os,time;subprocess.Popen([sys.executable,'-c',sys.argv[1],str(os.getpid()),sys.argv[2]]);time.sleep(60)"
    parent=subprocess.Popen([sys.executable,'-c',parent_code,child_code,str(pid_file)],cwd=ROOT,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    pid=None
    try:
        deadline=time.monotonic()+5
        while not pid_file.exists() and time.monotonic()<deadline:time.sleep(.02)
        assert pid_file.exists();pid=int(pid_file.read_text())
        child=psutil.Process(pid)
        def retired():
            try:return not child.is_running() or child.status()==psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:return True
        parent.kill();parent.wait(timeout=5)
        deadline=time.monotonic()+5
        while not retired() and time.monotonic()<deadline:time.sleep(.02)
        assert retired()
    finally:
        if parent.poll() is None:parent.kill();parent.wait(timeout=5)
        if pid is not None and psutil.pid_exists(pid) and psutil.Process(pid).status()!=psutil.STATUS_ZOMBIE:psutil.Process(pid).kill()
