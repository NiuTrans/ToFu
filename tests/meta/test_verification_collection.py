"""Verification guards collect independently of the application they supervise."""

import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests._jsdom import require_node_modules


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_runner_guard_collects_without_materializing_application_sources():
    probe = '''
import pytest
from tests import _jsdom

def refuse_application_build():
    raise AssertionError("runner contract collection must not build the application")

_jsdom.runtime_sections_dir = refuse_application_build
raise SystemExit(pytest.main([
    "tests/frontend/test_jsdom_runner_structured.py", "--collect-only", "-q", "-n", "0",
]))
'''
    result = subprocess.run(
        [sys.executable, '-c', probe], cwd=ROOT,
        env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'TestRunHarnessExpectPass' in result.stdout


@pytest.mark.parametrize(('paths', 'expected'), [
    (['tests/frontend/test_frontend_folder_longpress.py'], [
        'test_NC_longpress_timer_is_load_bearing', 'test_flow_picker_aria_roles_present',
    ]),
    (['tests/frontend/test_frontend_debug_request_log.py',
      'tests/frontend/test_frontend_markdown_escape_raw_html.py'], [
        'test_request_log_appends_per_round', 'test_neuter_recording_stripped_flips_red',
        'test_markdown_escape_raw_html_contract',
    ]),
], ids=['folder-controls', 'request-log-and-markdown'])
def test_focused_frontend_collects_without_materializing_unrelated_sections(paths, expected):
    require_node_modules('vite', 'jsdom')
    probe = f'''
import pytest
from tests import _runtime_sections

def refuse_whole_graph():
    raise AssertionError("focused tests must request only their owned runtime sections")

_runtime_sections.runtime_sections_dir = refuse_whole_graph
raise SystemExit(pytest.main([
    *{paths!r}, "--collect-only", "-q", "-n", "0",
]))
'''
    result = subprocess.run(
        [sys.executable, '-c', probe], cwd=ROOT,
        env={**os.environ, 'PYTHONPATH': str(ROOT), 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for name in expected:
        assert name in result.stdout
