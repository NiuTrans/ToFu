"""Paired real HTTP timing evidence; it does not authorize default promotion."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id():return 2


@pytest.mark.parametrize('cohort,kernel',[(cohort,kernel) for cohort in range(8)
    for kernel in ((False,True) if cohort%2==0 else (True,False))])
def test_native_graph_system_comparison(application_storage,tmp_path,monkeypatch,caplog,cohort,kernel):
    import os
    from pathlib import Path
    destination=os.environ.get('TOFU_NATIVE_FLOW_COMPARISON_RECEIPTS')
    if not destination:pytest.skip('explicit comparison artifact destination required')
    monkeypatch.setenv('TOFU_NATIVE_APPLICATION_RECEIPTS',str(Path(destination)/f'cohort-{cohort}'/('native' if kernel else 'python')))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_parallel=True,flow_child_model='beta',native_flow_kernel=kernel)
