"""Actual v4 application graph and role execution use the native runtime."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id():return 2


@pytest.mark.parametrize('parallel',[False,True])
@pytest.mark.parametrize('child_model',['alpha','beta'])
def test_native_graph_and_roles_close_before_one_wallet_settlement(
        application_storage,tmp_path,monkeypatch,caplog,parallel,child_model):
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_parallel=parallel,flow_child_model=child_model,native_flow_kernel=True)
