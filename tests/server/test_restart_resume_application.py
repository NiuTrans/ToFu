"""A real execution host generation resumes what a restart paused and tells the requester.

The isolated harness of ``test_execution_application.py``: a resident storage
service and a real headless production application in a private project; no
production server is touched. A helper process plays the stopping generation
(the Global Router's accepted restart and its open notice, a chat Turn the
shutdown quiesce recorded and settled); the execution host then boots as the
next generation. Its post-ready recovery must continue the chat from its
checkpoint and deliver exactly one restart-complete notice to the Router as a
new nonhuman Turn (docs/RUN_CONTINUITY.md, docs/ROUTER.md R9). No model runs:
the resumed Turns fail at the provider, after the evidence this spec reads.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from serverctl_pkg.execution_service import ExecutionService
from serverctl_pkg.native_storage_service import NativeStorageService
from serverctl_pkg.restart_budget import NATIVE_SERVICE_PREPARATION_SECONDS

pytestmark = [pytest.mark.api,
              pytest.mark.skipif(not sys.platform.startswith('linux'),
                                 reason='Linux process identity required')]
ROOT = Path(__file__).resolve().parents[2]
NOTICE_HEADER = '[Restart outcome: nonhuman notice, not the owner\u2019s words and not authority]'


def _probe(mode, environment):
    completed = subprocess.run([sys.executable, '-m', 'tests.support.restart_resume_probe', mode],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=240)
    assert completed.returncode == 0, completed.stdout[-4000:] + completed.stderr[-4000:]
    line = next(line for line in completed.stdout.splitlines() if line.startswith('PROBE '))
    return json.loads(line[len('PROBE '):])


def _wait(predicate, what, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError(f'{what} did not happen within {seconds}s')


def test_the_next_generation_resumes_the_chat_and_wakes_the_router_once(tmp_path):
    (tmp_path / 'contracts').mkdir()
    (tmp_path / 'contracts/native_storage_connection_v2.json').symlink_to(
        ROOT / 'contracts/native_storage_connection_v2.json')
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('TOFU_', 'TOFUDB_')) and key != 'LD_LIBRARY_PATH'}
    environment.update(PYTHONPATH=str(ROOT), TOFU_PROJECT_PATH=str(tmp_path),
        TOFU_DATA_DIR=str(tmp_path / 'data'), TOFU_DEPLOYMENT_MODE='personal',
        TOFU_PERSONAL_STORAGE_BACKEND='sqlite')
    storage = NativeStorageService(str(tmp_path), sys.executable)
    execution = ExecutionService(str(tmp_path), sys.executable)
    occupied = socket.socket()
    occupied.bind(('127.0.0.1', 0))
    occupied.listen()
    approvals = tmp_path / 'data' / 'lifecycle_approvals.json'
    try:
        plan = storage.prepare(environment)
        environment['TOFU_STORAGE_CONNECTION_FILE'] = storage.ensure(plan, environment)
        environment['TOFU_PROJECT_PATH'] = str(ROOT)
        environment['PORT'] = str(occupied.getsockname()[1])
        seeded = _probe('seed', environment)
        assert json.loads(approvals.read_text())['restartNotices'][0]['state'] == 'pending'

        execution_plan = execution.prepare(environment)
        execution.ensure(execution_plan, environment)
        record = _wait(lambda: execution.record() if execution.record().get('state') in {'ready', 'failed'}
                       else None, 'execution host readiness', NATIVE_SERVICE_PREPARATION_SECONDS)
        assert record.get('state') == 'ready', record
        # Post-ready recovery: the resume pass, then the restart notice.
        notice = _wait(lambda: next((item for item in json.loads(approvals.read_text())['restartNotices']
                                     if item['state'] not in ('pending', 'delivering')), None),
                       'restart notice delivery', 120)
        assert notice['state'] == 'delivered', notice
        execution.stop()

        result = _probe('read', environment)
        assert result['chatTurn'] == seeded['chatTurn']
        assert result['chatAttemptCommand'] == 'restart-resume:' + seeded['chatAttempt']
        assert result['chatAttemptOperation'] in ('continue', 'checkpoint_resume')
        notices = [item for item in result['routerInputs'] if item['content'].startswith(NOTICE_HEADER)]
        assert len(notices) == 1 and notices[0]['initiator'] == 'proactive'
        assert seeded['actionId'] in notices[0]['content'] and 'completed' in notices[0]['content']
        assert '- chat: resumed from its checkpoint (' in notices[0]['content']
        assert result['actionStatus'] == 'completed'
        assert result['notice']['conversationId'] == 'router'
        assert result['notice']['disposition'] in ('started', 'steered', 'queued')
    finally:
        try:
            execution.stop()
        finally:
            storage.stop()
            occupied.close()
