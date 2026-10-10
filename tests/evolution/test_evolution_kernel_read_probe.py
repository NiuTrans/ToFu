"""Real kernel reads stay distinct from initial hashes, mappings and semantics."""
import hashlib
from pathlib import Path
import shutil
import sys
import pytest
from scripts.trace_experiment_reads import observe

pytestmark = pytest.mark.unit


@pytest.fixture
def tracer():
    binary = shutil.which('strace')
    if not binary: pytest.skip('Optional Linux strace diagnostic is unavailable')
    return binary


def probe(tmp_path, tracer, source, selected=('dataset',)):
    return observe(tmp_path, selected, [sys.executable,'-S','-c',source], timeout_seconds=5, tracer=tracer)


def test_real_read_is_observed_but_untouched_control_is_not(tmp_path, tracer):
    (tmp_path/'dataset').write_bytes(b'held-out data')
    (tmp_path/'unused').write_bytes(b'not used')
    result = probe(tmp_path,tracer,"from pathlib import Path; assert Path('dataset').read_bytes() == b'held-out data'", ('dataset','unused'))
    assert result['returncode'] == 0 and result['trace_transport_complete']
    read, unused = result['inputs']
    assert read['read_calls'] >= 1 and read['read_bytes'] == 13
    assert read['initial_sha256'] == hashlib.sha256(b'held-out data').hexdigest()
    assert read['stable_through_probe'] and unused['read_calls'] == 0


def test_readable_mapping_does_not_claim_actual_read_bytes(tmp_path, tracer):
    (tmp_path/'dataset').write_bytes(b'x'*4096)
    result = probe(tmp_path,tracer,"import mmap; f=open('dataset','rb'); m=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ); assert m[0] == 120; m.close(); f.close()")
    assert result['returncode'] == 0, result
    assert result['inputs'][0]['readable_maps'] >= 1, result
    assert result['inputs'][0]['read_bytes'] == 0


def test_restoring_original_bytes_does_not_hide_generation_changes(tmp_path, tracer):
    (tmp_path/'dataset').write_bytes(b'original')
    result = probe(tmp_path,tracer,"from pathlib import Path; p=Path('dataset'); p.write_bytes(b'changed'); p.read_bytes(); p.write_bytes(b'original')")
    assert result['inputs'][0]['read_calls'] >= 1
    assert result['inputs'][0]['stable_through_probe'] is False


def test_timeout_terminates_probe_without_complete_evidence(tmp_path, tracer):
    (tmp_path/'dataset').write_bytes(b'bytes')
    result = observe(tmp_path,['dataset'],[sys.executable,'-c','import time;time.sleep(30)'],timeout_seconds=.1,tracer=tracer)
    assert result['timed_out'] and not result['trace_transport_complete']


def test_unavailable_tracer_never_executes_command(tmp_path):
    (tmp_path/'dataset').write_bytes(b'bytes')
    with pytest.raises(RuntimeError,match='not executed'):
        observe(tmp_path,['dataset'],[sys.executable,'-c',"open('marker','w').write('bad')"],timeout_seconds=1,tracer=str(tmp_path/'missing'))
    assert not (tmp_path/'marker').exists()


def test_unicode_selected_path_is_decoded_from_kernel_trace(tmp_path, tracer):
    (tmp_path/'数据').write_bytes(b'bytes')
    result = probe(tmp_path,tracer,"from pathlib import Path; Path('数据').read_bytes()", ('数据',))
    assert result['inputs'][0]['read_bytes'] == 5


def test_timeout_stops_detached_traced_descendant(tmp_path, tracer):
    import os
    import time
    import signal
    (tmp_path/'dataset').write_bytes(b'bytes')
    parent = "import os,time\npid=os.fork()\nif pid==0:\n os.setsid();f=os.open('child-pid',os.O_CREAT|os.O_WRONLY,0o600);os.write(f,str(os.getpid()).encode());os.close(f);time.sleep(30)\nelse:\n time.sleep(30)"
    result = observe(tmp_path,['dataset'],[sys.executable,'-S','-c',parent],timeout_seconds=2,tracer=tracer)
    assert result['timed_out']
    pid = int((tmp_path/'child-pid').read_text())
    try:
        deadline = time.monotonic()+2
        while time.monotonic()<deadline:
            status = Path('/proc',str(pid),'stat')
            if not status.exists() or status.read_text().split(') ',1)[1].startswith('Z '): break
            time.sleep(.02)
        else: pytest.fail('Detached tracee survived probe timeout')
    finally:
        try: os.kill(pid,signal.SIGKILL)
        except ProcessLookupError: pass


@pytest.mark.parametrize('selected', ['../outside', '.env', 'link'])
def test_private_or_unsafe_selection_refuses_before_command(tmp_path, tracer, selected):
    from lib.evolution.contracts import EvolutionStateError
    (tmp_path/'dataset').write_bytes(b'bytes');(tmp_path/'link').symlink_to(tmp_path/'dataset')
    with pytest.raises((EvolutionStateError,OSError)):
        observe(tmp_path,[selected],[sys.executable,'-c',"open('marker','w').write('bad')"],timeout_seconds=1,tracer=tracer)
    assert not (tmp_path/'marker').exists()
