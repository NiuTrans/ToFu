"""Real v4 ROOT folds settled Rust child fees into the original wallet invoice."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    # The first wallet user is owner 2, distinct from the local owner 1.
    return 2


@pytest.mark.parametrize('priced_child_depth', [1, 2])
def test_native_priced_child_wallet_transfer(application_storage,tmp_path,monkeypatch,caplog,priced_child_depth):
    native_case(application_storage,tmp_path,monkeypatch,caplog,
        scenario='mixed_direct_first',prebind_failure=False,resume_operation=None,priced_child=True,priced_child_depth=priced_child_depth)
