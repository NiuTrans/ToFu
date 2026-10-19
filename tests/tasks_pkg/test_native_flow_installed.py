"""Real v4 Flow leaves use native invocation and original root wallet quotes."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


@pytest.mark.parametrize('parallel', [False, True])
def test_native_flow_roles_and_wallet_settlement(
        application_storage, tmp_path, monkeypatch, caplog, parallel):
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        native_flow=True, flow_parallel=parallel)
