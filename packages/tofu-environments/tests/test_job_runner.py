"""Real subprocess verification of execution deadlines and result seals."""
import hashlib
import os as os
import sys
import pytest
from tofu_environments.job_runner import run_job
pytestmark=pytest.mark.unit

def spec(code,outputs=(),seconds=3,maximum=4096):
    return {'command':[sys.executable,'-c',code],'outputs':list(outputs),
            'limits':{'runtime_seconds':seconds,'result_bytes':maximum}}

def test_execution_and_output_digest(tmp_path):
    result=run_job(spec("from pathlib import Path; Path('result.json').write_text('{}');print('done')",['result.json']),tmp_path)
    assert result['state']=='succeeded' and result['exit_code']==0
    assert result['stdout']=='done\n'
    assert result['outputs']==[{'name':'result.json','bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest()}]

def test_nonzero_exit_not_success(tmp_path):
    result=run_job(spec('raise SystemExit(9)'),tmp_path)
    assert result['state']=='failed' and result['exit_code']==9

def test_timeout_stops_real_process(tmp_path):
    result=run_job(spec('import time;time.sleep(30)',seconds=1),tmp_path)
    assert result['error']=='runtime_limit' and result['elapsed_seconds']<5

def test_stdout_limit_stops_process(tmp_path):
    result=run_job(spec("print('x'*10000)",maximum=20),tmp_path)
    assert result['error']=='result_limit'
    assert len(result['stdout'].encode())<=20

def test_file_output_shares_total_budget(tmp_path):
    result=run_job(spec("from pathlib import Path;Path('x').write_bytes(b'x'*100);print('hello')",['x'],maximum=50),tmp_path)
    assert result['error']=='result_limit' and result['state']=='failed'

@pytest.mark.parametrize('name',['../escape','/absolute','missing'])
def test_unsafe_or_missing_results_rejected(tmp_path,name):
    result=run_job(spec('pass',[name]),tmp_path)
    assert result['state']=='failed'

def test_symlink_output_rejected(tmp_path):
    (tmp_path/'target').write_text('safe');(tmp_path/'link').symlink_to(tmp_path/'target')
    assert run_job(spec('pass',['link']),tmp_path)['error']=='invalid_output'

def test_leader_exit_does_not_leave_descendant_holding_pipe(tmp_path):
    code="import os,time;pid=os.fork();time.sleep(30) if pid==0 else None"
    result=run_job(spec(code,seconds=1),tmp_path)
    assert result['error']=='runtime_limit' and result['elapsed_seconds']<5
