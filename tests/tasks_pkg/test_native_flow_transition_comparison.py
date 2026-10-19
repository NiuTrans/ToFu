"""Paired complete HTTP requests compare old and combined native cursor calls."""
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


@pytest.mark.parametrize('cohort,combined',[(cohort,combined) for cohort in range(8)
    for combined in ((False,True) if cohort%2==0 else (True,False))])
def test_complete_native_flow_request_transition_comparison(
        application_storage,tmp_path,monkeypatch,caplog,cohort,combined):
    old=os.environ.get('TOFU_NATIVE_FLOW_BASELINE_LIBRARY')
    new=os.environ.get('TOFU_NATIVE_FLOW_CANDIDATE_LIBRARY')
    destination=os.environ.get('TOFU_NATIVE_FLOW_TRANSITION_RECEIPTS')
    if not all((old,new,destination)):pytest.skip('explicit paired libraries and destination required')
    arm='combined' if combined else 'legacy'
    monkeypatch.setenv('TOFU_NATIVE_FLOW_TEST_LIBRARY',new if combined else old)
    monkeypatch.setenv('TOFU_NATIVE_APPLICATION_RECEIPTS',str(Path(destination)/f'cohort-{cohort}'/arm))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_parallel=True,flow_child_model='beta',native_flow_kernel=True)
