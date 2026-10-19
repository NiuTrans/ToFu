"""Ordinary paid tree Continue restores spend without owning a native journal."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 2


@pytest.mark.parametrize('operation',['billing_continue','billing_checkpoint_resume'])
def test_native_priced_tree_continue_restores_original_invoice_consumption(
        application_storage,tmp_path,monkeypatch,caplog,operation):
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='mixed_direct_first',
        prebind_failure=False,resume_operation=operation,priced_child=True,priced_child_depth=2)
