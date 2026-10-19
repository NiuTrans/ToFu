"""Real worker/critic/worker/critic loops use the native verifier policy."""
import pytest
from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)
pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id():return 2


@pytest.mark.parametrize('first_verdict',['[VERDICT: CONTINUE_WORKER]','[VERDICT: STOP] still failing'],ids=['explicit-continue','unresolved-stop'])
def test_native_loop_preserves_continuation_terminal_and_original_fee_rows(
        application_storage,tmp_path,monkeypatch,caplog,first_verdict):
    native_case(application_storage,tmp_path,monkeypatch,caplog,scenario='text',
        prebind_failure=False,resume_operation=None,native_flow=True,
        flow_child_model='beta',native_flow_kernel=True,flow_verifier_loop=True,flow_first_verdict=first_verdict)
