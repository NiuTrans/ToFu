"""Interleaved complete v4 requests compare native versus retained branch policy."""
import ctypes
import os
from pathlib import Path
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api

@pytest.fixture
def application_storage_owner_id():return 2

@pytest.mark.parametrize('cohort,native_branch',[(cohort,arm) for cohort in range(8)
    for arm in ((False,True) if cohort%2==0 else (True,False))])
def test_complete_branch_policy_comparison(application_storage,tmp_path,monkeypatch,caplog,cohort,native_branch):
    old=os.environ.get('TOFU_NATIVE_FLOW_BASELINE_LIBRARY')
    new=os.environ.get('TOFU_NATIVE_FLOW_CANDIDATE_LIBRARY')
    destination=os.environ.get('TOFU_NATIVE_BRANCH_RECEIPTS')
    if not all((old,new,destination)):pytest.skip('explicit paired libraries and receipt destination required')
    assert (getattr(ctypes.CDLL(new if native_branch else old),'tofu_flow_branch_choose',None) is not None)==native_branch
    arm='native' if native_branch else 'python'
    monkeypatch.setenv('TOFU_NATIVE_FLOW_TEST_LIBRARY',new if native_branch else old)
    monkeypatch.setenv('TOFU_NATIVE_APPLICATION_RECEIPTS',str(Path(destination)/f'cohort-{cohort}'/arm))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_child_model='beta',native_flow_kernel=True,flow_branch=True)
