"""Request-scope image admission preserves real ROOT work and rollback."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api
@pytest.fixture
def application_storage_owner_id():return 1
@pytest.mark.parametrize('prebind_failure',[False,True])
def test_explicit_image_preparation_keeps_paid_work_and_prebind_failure(
        application_storage,tmp_path,monkeypatch,caplog,prebind_failure):
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=prebind_failure,resume_operation=None,image_preparation=True)
