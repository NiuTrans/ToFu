"""Descriptor arguments retain selected input identity across path replacement."""
import sys
import shutil
import shlex
import pytest
from lib.project_mod.file_read_probe import run_observed_process
from lib.project_mod.run_command import run_command_process, _get_cmd_env

pytestmark = pytest.mark.unit


@pytest.fixture
def tracer():
    binary = shutil.which('strace')
    if not binary: pytest.skip('Optional Linux strace adapter unavailable')
    return binary


def test_pinned_argument_reads_original_after_directory_swap(tmp_path, tracer):
    (tmp_path/'source').mkdir();(tmp_path/'source/data').write_bytes(b'original')
    code = "import os,sys,json;os.rename('source','saved');os.mkdir('source');open('source/data','wb').write(b'substitute');print(json.dumps({'read':open(sys.argv[1],'rb').read().decode()}))"
    argv = [sys.executable,'-S','-c',code,'@tofu-input:source/data']
    report = run_observed_process(tmp_path,['source/data'],argv,timeout_seconds=5,
        child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,pin_inputs=True)
    from lib.research.action import _observed_tool_payload
    payload, state = _observed_tool_payload({'toolName':'run_command','toolArgs':{'command':shlex.join(argv)}},report['command_result'])
    assert state == 'done' and payload['read'] == 'original'
    assert report['inputs'][0]['descriptor_supplied'] is True
    assert report['input_binding'] == 'borrowed_readonly_descriptors'
    assert report['inputs'][0]['stable_through_probe'] is False  # The path now refers to another file.
    assert report['inputs'][0]['descriptor_metadata_stable'] is True
    assert report['inputs'][0]['final_path_matches_initial_descriptor'] is False


def test_only_referenced_selected_descriptors_are_supplied(tmp_path, tracer):
    (tmp_path/'data').write_bytes(b'bytes');(tmp_path/'unused').write_bytes(b'unused')
    report = run_observed_process(tmp_path,['data','unused'],[sys.executable,'-S','-c',"import sys;assert open(sys.argv[1],'rb').read()==b'bytes'",'@tofu-input:data'],
        timeout_seconds=5,child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,pin_inputs=True)
    assert report['returncode'] == 0
    assert [row['descriptor_supplied'] for row in report['inputs']] == [True,False]


@pytest.mark.parametrize('token', ['@tofu-input:missing', "prefix@tofu-input:data", 'data'])
def test_invalid_or_missing_binding_tokens_refuse_before_execution(tmp_path, tracer, token):
    (tmp_path/'data').write_bytes(b'bytes')
    with pytest.raises(ValueError):
        run_observed_process(tmp_path,['data'],[sys.executable,'-c',"open('marker','w').write('bad')",token],
            timeout_seconds=5,child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,pin_inputs=True)
    assert not (tmp_path/'marker').exists()


@pytest.mark.parametrize('descriptors', [[True],[0],[-1]])
def test_invalid_borrowed_descriptors_refuse_before_allocation(tmp_path, descriptors):
    with pytest.raises(ValueError,match='descriptors'):
        run_command_process([sys.executable,'-c',"open('marker','w').write('bad')"],command='invalid descriptors',
            timeout=1,base=str(tmp_path),child_env=_get_cmd_env(str(tmp_path)),extra_pass_fds=descriptors)
    assert not (tmp_path/'marker').exists()


def test_binding_does_not_claim_immutable_contents(tmp_path, tracer):
    (tmp_path/'data').write_bytes(b'original')
    code = "import sys;open(sys.argv[1],'wb').write(b'changed');assert open(sys.argv[1],'rb').read()==b'changed'"
    report = run_observed_process(tmp_path,['data'],[sys.executable,'-S','-c',code,'@tofu-input:data'],
        timeout_seconds=5,child_env=_get_cmd_env(str(tmp_path)),tracer=tracer,pin_inputs=True)
    assert report['returncode'] == 0
    assert report['inputs'][0]['stable_through_probe'] is False
    assert 'in-place edits remain possible' in report['binding_scope']


def test_borrowed_fds_preserve_leases_and_remain_caller_owned(tmp_path, monkeypatch):
    import os
    from lib.project_mod import run_command
    (tmp_path/'lease').write_bytes(b'lease');(tmp_path/'data').write_bytes(b'data')
    lease = os.open(tmp_path/'lease',os.O_RDONLY);data = os.open(tmp_path/'data',os.O_RDONLY)
    try:
        monkeypatch.setattr(run_command,'workspace_lease_fds',lambda:(lease,))
        code = "import os,sys;assert os.read(int(sys.argv[1]),5)==b'lease';assert os.read(int(sys.argv[2]),4)==b'data'"
        result = run_command_process([sys.executable,'-S','-c',code,str(lease),str(data)],
            command='lease and borrowed inputs',timeout=5,base=str(tmp_path),child_env=_get_cmd_env(str(tmp_path)),extra_pass_fds=(data,))
        assert '[exit code: 0]' in result
        os.fstat(lease);os.fstat(data)
        assert not os.get_inheritable(data)
    finally:
        os.close(lease);os.close(data)
