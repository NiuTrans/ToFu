"""Production profile reaches the same public ROOT route on each storage owner."""
import pytest

from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 1


def test_public_root_uses_complete_production_profile(application_storage, tmp_path, monkeypatch, caplog):
    from lib.agent_core.native_process import Budget
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_production_profile import compile_native_application_profile

    def configure(*, configuration, **options):
        # The installed-route harness owns this explicit small transport. The
        # production compiler must honor it while retaining real route, account,
        # price, conversation and selected-storage authorities.
        profile = compile_native_application_profile(transport=Budget(1, 16 << 20, 1 << 20, 1 << 20, 5),
            launch_policy={'frame_maximum': 1 << 20})
        return NativeRootConfiguration(configuration=profile, **options)

    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        application_configuration_factory=configure)
