"""Real v4 classifier and selected role use native branch policy and fee lineage."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api

@pytest.fixture
def application_storage_owner_id():return 2


def test_native_classifier_selects_one_role_without_python_matching(application_storage,tmp_path,monkeypatch,caplog):
    monkeypatch.setattr('lib.orchestration.branch_runtime.OrchestrationBranchRuntime._classifier_choice',
        lambda *a:pytest.fail('native branch entered Python matching'))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_child_model='beta',native_flow_kernel=True,flow_branch=True)
