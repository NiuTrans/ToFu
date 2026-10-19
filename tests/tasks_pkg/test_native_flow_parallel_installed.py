"""Real v4 parallel roles use native branch plan and joined text merge."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api

@pytest.fixture
def application_storage_owner_id():return 2


def test_native_parallel_roles_borrow_spans_without_python_or_json_merge(application_storage,tmp_path,monkeypatch,caplog):
    monkeypatch.setattr('lib.orchestration.native_flow._LOOP_ENCODER',None)
    monkeypatch.setattr('lib.orchestration.parallel_runtime.OrchestrationParallelRuntime._merge',
        lambda *a:pytest.fail('native parallel entered Python merge'))
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_parallel=True,flow_child_model='beta',native_flow_kernel=True)
