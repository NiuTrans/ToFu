"""Cross-test isolation: an in-process ``tofu-agent run`` cannot starve later dispatch.

``tofu_agent.cli`` gives the one-shot ``run`` process the headless default
``TOFU_DISABLE_CONFIGURED_SLOTS=1`` through ``os.environ``. Tests that call the
CLI in-process share their pytest worker with every later test, and the
switch made the personal configured v2 pool build zero slots. Replay
scenarios that ran later on that worker failed with ``no_slot`` instead of
reaching their recorded provider (the unit gate failed only on xdist
assignments that put both modules on one worker).

The spec reruns that sequence in one dedicated, serial pytest process:
the run-CLI contract module first, then the upstream replay scenario whose
typed ``upstream_error`` the leak replaced with ``no_slot``.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ElementTree

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_HEADLESS_RUN_MODULE = 'tests/tofu_agent/test_tofu_agent_run_cli.py'
_REPLAY_SCENARIO = ('tests/llm/test_upstream_turn_replay.py::'
                    'test_persistent_upstream_outage_exhausts_once_and_keeps_the_failure')


def _standalone_session_environment() -> dict[str, str]:
    """Let the nested session create its own worker roots and replay mode.

    Worker identity and ``TOFU_PYTEST_*`` root keys would make the nested
    conftest join this worker's data and storage roots. A replay author or
    record mode would make both sessions write one ``script.next.jsonl``.
    """
    inherited_session_state = (
        'PYTEST_XDIST_', 'PYTEST_CURRENT_TEST', 'PYTEST_ADDOPTS',
        'TOFU_PYTEST_', 'TOFU_LLM_REPLAY')
    return {name: value for name, value in os.environ.items()
            if not name.startswith(inherited_session_state)}


def test_replay_dispatch_survives_an_earlier_in_process_headless_run(tmp_path):
    report_path = tmp_path / 'session.xml'
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
         '-p', 'no:randomly', '-n', '0', '-q', '--timeout=240',
         f'--junitxml={report_path}',
         _HEADLESS_RUN_MODULE, _REPLAY_SCENARIO],
        cwd=_ROOT, env=_standalone_session_environment(),
        capture_output=True, text=True, timeout=280)

    report = completed.stdout[-6000:] + completed.stderr[-2000:]
    assert completed.returncode == 0, report
    replay_cases = [
        case for case in ElementTree.parse(report_path).getroot().iter('testcase')
        if case.get('classname') == 'tests.llm.test_upstream_turn_replay']
    assert replay_cases, report
    unverified = {
        case.get('name'): [child.tag for child in case
                           if child.tag in {'failure', 'error', 'skipped'}]
        for case in replay_cases}
    assert not any(unverified.values()), (unverified, report)
