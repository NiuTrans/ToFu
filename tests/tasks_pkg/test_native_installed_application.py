"""Published executable selection reaches ordinary durable ROOT and tools."""
import pytest
from tests.storage.test_storage_application_contract import application_storage as application_storage
from tests.tasks_pkg.test_native_application_installed import test_http_application_issues_native_root_without_task_sentinel as native_case

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id(): return 1


@pytest.mark.parametrize('scenario', ['text', 'mixed_direct_first'])
def test_installed_executable_application(application_storage, tmp_path, monkeypatch, caplog, scenario):
    native_case(application_storage, tmp_path, monkeypatch, caplog,
                scenario=scenario, prebind_failure=False, resume_operation=None,
                installed_executable=True)


@pytest.mark.parametrize('scenario', ['text', 'mixed_direct_first'])
def test_installed_token_library_application(application_storage, tmp_path, monkeypatch, caplog, scenario):
    native_case(application_storage, tmp_path, monkeypatch, caplog,
                scenario=scenario, prebind_failure=False, resume_operation=None,
                installed_executable=True, installed_token_library=True)
