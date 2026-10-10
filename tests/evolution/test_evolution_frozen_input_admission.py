"""Caller-frozen file digests refuse wrong or changed versions before effects."""
import hashlib
import shutil
import sys
import pytest
from lib.project_mod.file_read_probe import run_observed_process
from lib.project_mod.run_command import _get_cmd_env

pytestmark = pytest.mark.unit


@pytest.fixture
def tracer():
    value = shutil.which('strace')
    if not value: pytest.skip('Optional Linux strace unavailable')
    return value


def run(tmp_path, tracer, expected):
    return run_observed_process(tmp_path,['data'],[sys.executable,'-S','-c',"import sys;assert open(sys.argv[1],'rb').read()==b'original'",'@tofu-input:data'],
        timeout_seconds=5,child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,pin_inputs=True,expected_input_sha256=expected)


def test_expected_generation_is_verified_before_pinned_execution(tmp_path, tracer):
    (tmp_path/'data').write_bytes(b'original');expected = hashlib.sha256(b'original').hexdigest()
    report = run(tmp_path,tracer,{'data':expected})
    assert report['returncode'] == 0
    row = report['inputs'][0]
    assert row['initial_sha256'] == row['expected_sha256'] == expected
    assert row['matches_expected'] is True and row['descriptor_supplied'] is True


@pytest.mark.parametrize('expected', [{'data':'0'*64}, {}, {'missing':'0'*64}, {'data':'BAD'}, {'data':True}])
def test_wrong_or_incomplete_expected_inputs_never_allocate_process(tmp_path, tracer, expected):
    (tmp_path/'data').write_bytes(b'original')
    with pytest.raises((ValueError,RuntimeError)):
        run_observed_process(tmp_path,['data'],[sys.executable,'-S','-c',"open('marker','w').write('bad')"],
            timeout_seconds=5,child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,expected_input_sha256=expected)
    assert not (tmp_path/'marker').exists()


def test_mutation_after_hash_before_allocation_is_refused(tmp_path, tracer, monkeypatch):
    from lib.project_mod import file_read_probe
    path = tmp_path/'data';path.write_bytes(b'original');expected = hashlib.sha256(b'original').hexdigest()
    original = file_read_probe.os.lseek
    def mutate(*args):
        path.write_bytes(b'changed');return original(*args)
    monkeypatch.setattr(file_read_probe.os,'lseek',mutate)
    with pytest.raises(RuntimeError,match='changed before launch'):
        run(tmp_path,tracer,{'data':expected})


def test_expected_mapping_is_frozen_from_callers_later_mutation(tmp_path, tracer, monkeypatch):
    from lib.project_mod import file_read_probe
    path = tmp_path/'data';path.write_bytes(b'original');digest = hashlib.sha256(b'original').hexdigest()
    expected = {'data':digest};original = file_read_probe.os.read
    def mutate(*args):
        result = original(*args);expected['data'] = '0'*64;return result
    monkeypatch.setattr(file_read_probe.os,'read',mutate)
    report = run(tmp_path,tracer,expected)
    assert report['returncode'] == 0 and report['inputs'][0]['expected_sha256'] == digest


@pytest.mark.parametrize('paths', ['data', [], ['data',3]])
def test_unstructured_selection_never_allocates_command(tmp_path, tracer, paths):
    (tmp_path/'data').write_bytes(b'original')
    with pytest.raises(ValueError):
        run_observed_process(tmp_path,paths,[sys.executable,'-S','-c',"open('marker','w').write('bad')"],
            timeout_seconds=5,tracer=tracer,child_env=_get_cmd_env(str(tmp_path)))
    assert not (tmp_path/'marker').exists()
