"""Caller schema arrays enter the real application ROOT, gateway and settlement."""
from copy import deepcopy

import pytest

from tests.tasks_pkg.test_native_application_installed import (
    application_storage as application_storage,
    test_http_application_issues_native_root_without_task_sentinel as native_case,
)

pytestmark = pytest.mark.api


@pytest.fixture
def application_storage_owner_id():
    return 1


def test_schema_array_survives_native_tools_and_durable_completion(
        application_storage, tmp_path, monkeypatch, caplog):
    from lib.tools.todo import TODO_WRITE_TOOL

    native_case(application_storage, tmp_path, monkeypatch, caplog,
        scenario='mixed_direct_first', prebind_failure=False, resume_operation=None,
        caller_tool_schemas=[deepcopy(TODO_WRITE_TOOL)])
