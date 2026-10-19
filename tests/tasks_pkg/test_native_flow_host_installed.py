"""Real v4 Flow owns release admission and unload through application shutdown."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api

@pytest.fixture
def application_storage_owner_id():return 2


def test_application_loads_and_releases_its_native_flow_archive(application_storage,tmp_path,monkeypatch,caplog):
    monkeypatch.setattr('lib.orchestration.branch_runtime.OrchestrationBranchRuntime._classifier_choice',
        lambda *a:pytest.fail('native branch entered Python matching'))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_child_model='beta',native_flow_kernel=True,flow_branch=True,flow_archive=True)
