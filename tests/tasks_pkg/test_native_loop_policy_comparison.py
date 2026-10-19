"""Interleaved complete HTTP loops over pinned native policy and prior libraries."""
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
@pytest.mark.parametrize('cohort,native',[(i,arm) for i in range(8)
    for arm in ((False,True) if i%2==0 else (True,False))])
def test_complete_http_loop_policy_comparison(application_storage,tmp_path,monkeypatch,caplog,cohort,native):
    old=os.environ.get('TOFU_NATIVE_FLOW_BASELINE_LIBRARY');new=os.environ.get('TOFU_NATIVE_FLOW_CANDIDATE_LIBRARY')
    destination=os.environ.get('TOFU_NATIVE_FLOW_LOOP_RECEIPTS')
    if not all((old,new,destination)):pytest.skip('explicit pinned pair and receipt destination required')
    monkeypatch.setenv('TOFU_NATIVE_FLOW_TEST_LIBRARY',new if native else old)
    monkeypatch.setenv('TOFU_NATIVE_APPLICATION_RECEIPTS',str(Path(destination)/f'cohort-{cohort}'/('native' if native else 'prior')))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_child_model='beta',native_flow_kernel=True,flow_verifier_loop=True)
