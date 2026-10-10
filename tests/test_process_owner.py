"""Local executor death requires kernel evidence, including unreaped exits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import lib.process_owner as owners

pytestmark = pytest.mark.unit


def test_unreaped_process_exit_is_dead_and_cold_research_task_is_interrupted(monkeypatch):
    if not hasattr(os, 'WNOWAIT') or owners.process_owner() is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    code = ('import json,sys; from lib.process_owner import process_owner; '
            'print(json.dumps(process_owner()),flush=True); sys.stdin.readline()')
    child = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        stamp = json.loads(child.stdout.readline())
        assert owners.owner_state(stamp) == 'alive'
        from tests.research.test_research_execution_owner import repository
        from tests.research.test_research_action_observations import MemoryRecords
        from lib.research.action_terminal import load_public_action_task
        client = MemoryRecords()
        monkeypatch.setattr('lib.research.execution_owner.process_owner', lambda: stamp)
        repository(client).claim_initial()
        monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
        assert load_public_action_task('research_action_observed', user_id=1) is None
        child.stdin.write('\n'); child.stdin.flush()
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        # The PID still exists, but this process can never execute again.
        assert Path(f'/proc/{child.pid}/stat').exists()
        assert owners.owner_state(stamp) == 'dead'
        task = load_public_action_task('research_action_observed', user_id=1)
        assert task['status'] == 'interrupted' and task['replay_allowed'] is False
        child.wait(timeout=5)
        assert owners.owner_state(stamp) == 'dead'
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)


@pytest.mark.parametrize('state,expected', [('R','alive'), ('S','alive'), ('D','alive'),
    ('T','alive'), ('t','alive'), ('I','alive'), ('K','alive'), ('W','alive'), ('P','alive'),
    ('Z','dead'), ('X','dead'), ('x','dead'), ('?','unknown'), ('ZZ','unknown')])
def test_kernel_state_with_same_start_time(state, expected, monkeypatch):
    stamp = {'host':'local','boot':'boot','pid_namespace':'namespace','pid':31,'started':'555'}
    monkeypatch.setattr(owners, 'process_owner', lambda: stamp)
    fields = [state] + ['0'] * 18 + ['555']; fields[17] = '1'
    stat = '31 (worker ) name) ' + ' '.join(fields)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
        stat if str(path) == '/proc/31/stat' else original(path, *args, **kwargs))
    assert owners.owner_state(stamp) == expected


def test_exited_leader_with_live_worker_retains_execution_authority():
    if owners.process_owner() is None:
        pytest.skip('local Linux process incarnation evidence unavailable')
    code = ('import ctypes,json,time,threading; from lib.process_owner import process_owner; '
            'threading.Thread(target=lambda: time.sleep(60)).start(); '
            'print(json.dumps(process_owner()),flush=True); ctypes.CDLL(None).pthread_exit(None)')
    child = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True)
    try:
        stamp = json.loads(child.stdout.readline())
        deadline = time.monotonic() + 5
        while True:
            fields = Path(f'/proc/{child.pid}/stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] == 'Z' or time.monotonic() >= deadline: break
            time.sleep(.01)
        assert fields[0] == 'Z' and int(fields[17]) > 1
        assert owners.owner_state(stamp) == 'alive'
    finally:
        child.kill(); child.wait(timeout=5); child.stdout.close()


@pytest.mark.parametrize('state,threads,expected', [('Z','2','alive'), ('X','2','alive'),
    ('x','2','alive'), ('R','0','unknown'), ('Z','0','unknown'), ('Z','-1','unknown'),
    ('R','invalid','unknown')])
def test_thread_count_fences_whole_process_exit(state, threads, expected, monkeypatch):
    stamp = {'host':'local','boot':'boot','pid_namespace':'namespace','pid':31,'started':'555'}
    monkeypatch.setattr(owners, 'process_owner', lambda: stamp)
    fields = [state] + ['0'] * 18 + ['555']; fields[17] = threads
    stat = '31 (worker) ' + ' '.join(fields)
    original = Path.read_text
    monkeypatch.setattr(Path, 'read_text', lambda path, *args, **kwargs:
        stat if str(path) == '/proc/31/stat' else original(path, *args, **kwargs))
    assert owners.owner_state(stamp) == expected
