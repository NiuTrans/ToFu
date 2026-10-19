"""Ordinary v4 native ROOT can wait for headers under its task-owned policy."""
import os
from pathlib import Path
import time

import pytest

from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 1


@pytest.mark.parametrize('production_policy', [False, True])
def test_native_root_waits_for_late_headers_before_starting_stream_idle_budget(
        application_storage, tmp_path, monkeypatch, caplog, production_policy):
    from benchmarks import native_agent_root
    from lib.tasks_pkg.native_application import NativeRootConfiguration

    receipt_root = os.environ.get('TOFU_NATIVE_APPLICATION_RECEIPTS')
    if receipt_root:
        # Keep this delayed-response proof distinct from the ordinary text
        # fixture's receipts when both cases run in the same invocation.
        monkeypatch.setenv('TOFU_NATIVE_APPLICATION_RECEIPTS',
            str(Path(receipt_root) / ('production-http-policy' if production_policy else 'late-headers')))

    original_workload = native_agent_root.RootWorkload
    responses = []

    def delayed_workload(*args, **kwargs):
        workload = original_workload(*args, **kwargs)

        def delayed_text(ordinal):
            # RootWorkload calls this after reading the complete request body
            # and before sending response headers on the actual TCP socket.
            assert ordinal == 0
            started = time.monotonic()
            # Exceed the fixture's historical two-second header limit as well
            # as the thirty-millisecond stream-idle budget selected below.
            time.sleep(2.15)
            responses.append(time.monotonic() - started)
            return 'durable fixture answer'

        workload.response_text = delayed_text
        return workload

    def configure(*, configuration, **options):
        if production_policy:
            from lib.tasks_pkg.native_http_policy import compile_native_http_policy
            configuration['policy'] = compile_native_http_policy()
        else:
            configuration['policy']['request_headers_ms'] = None
            configuration['policy']['stream_idle_ms'] = 30
            configuration['policy']['write_ms'] = 30
        return NativeRootConfiguration(configuration=configuration, **options)

    monkeypatch.setattr(native_agent_root, 'RootWorkload', delayed_workload)
    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='text', prebind_failure=False, resume_operation=None,
        application_configuration_factory=configure)
    assert len(responses) == 1 and responses[0] >= 2.15
