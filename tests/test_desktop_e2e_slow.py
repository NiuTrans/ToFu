#!/usr/bin/env python3
"""RWA slow e2e — REAL agent subprocess × REAL server, full write safety loop.

The design-doc §7 acceptance that was never入库: no in-process fakes. A real
Hypercorn serves the real app (``live_server`` fixture), a real
``python -m lib.desktop_agent`` subprocess polls it over real HTTP with a real
owner-scoped bridge credential, and the test drives the in-process bridge exactly the way the
LLM tool handlers do (``send_desktop_command``). Covered end-to-end:

  read → write → snapshot-on-disk → external edit → freshness refusal →
  re-read → write allowed → apply_diff → streamed run_command →
  path-escape refusal → 401 without the secret (auth seam over the wire).

Run (opt-in; excluded from unit/api tiers by the ``slow`` marker):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_desktop_e2e_slow.py -q
"""

import json
import os
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

pytestmark = pytest.mark.slow

_ROOT_NAME = 'e2eapp'


def _wait_agent_online(deadline_s=30.0):
    """Wait for OUR subprocess agent — identified by its share-root name, so a
    stale registration from another suite in the same pytest process (15s
    liveness window) can never be mistaken for it."""
    from lib.desktop import bridge as db
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        for a in db.online_agents():
            names = [r.get('name') for r in a.get('share_roots') or []
                     if isinstance(r, dict)]
            if _ROOT_NAME in names:
                return a
        time.sleep(0.2)
    return None


def _send(cmd_type, params, agent_id, timeout=30, cmd_id=None):
    from lib.desktop import bridge as db
    result, error = db.send_desktop_command(
        cmd_type, params, timeout=timeout,
        target_agent_id=agent_id, cmd_id=cmd_id, user_id='1')
    return result, error


@pytest.mark.slow
def test_remote_worktree_full_loop_real_subprocess(
        live_server, tmp_path, monkeypatch):
    import requests
    from lib.desktop import bridge as db

    from lib.api_keys import create_key
    _credential, bridge_token = create_key(
        owner_user_id=1,
        name='desktop-e2e-agent',
        scopes=['agents:bridge'],
    )

    # ── Local "user machine": a share root with one seed file ──
    root = tmp_path / 'worktree'
    root.mkdir()
    seed = root / 'README.md'
    seed.write_text('# app\nhello world\n', encoding='utf-8')
    agent_cfg = tmp_path / 'agent.json'
    agent_cfg.write_text(json.dumps(
        {'share_roots': [{'name': _ROOT_NAME, 'path': str(root)}]}),
        encoding='utf-8')

    # ── Auth seam over the REAL wire: no secret → 401 ──
    r = requests.post(f'{live_server}/api/desktop/poll', json={}, timeout=10)
    assert r.status_code == 401, r.text[:200]

    # ── Boot the REAL agent subprocess ──
    env = dict(os.environ)
    env['TOFU_DESKTOP_CONFIG'] = str(agent_cfg)
    agent_log = open(tmp_path / 'agent.log', 'w', encoding='utf-8')  # noqa: SIM115
    proc = subprocess.Popen(
        [sys.executable, '-m', 'lib.desktop_agent',
         '--server', live_server,
         '--allow-write', '--allow-exec',
         '--bridge-token', bridge_token,
         '--poll-interval', '0.5'],
        cwd=_REPO_ROOT, env=env,
        stdout=agent_log, stderr=subprocess.STDOUT)

    try:
        agent = _wait_agent_online(30.0)
        assert agent is not None, (
            'agent never registered — log tail:\n'
            + open(tmp_path / 'agent.log', encoding='utf-8').read()[-2000:])
        aid = agent['agent_id']
        # v2 registration carried the declared share roots to the registry.
        assert agent['share_roots'] == [
            {'name': _ROOT_NAME, 'path': str(root)}], agent

        # 1) read (arms the freshness token ON THE AGENT)
        res, err = _send('project_read_files',
                         {'root': _ROOT_NAME, 'path': 'README.md'}, aid)
        assert err is None and 'hello world' in res['content']

        # 2) write v2 → snapshot exists on the LOCAL disk
        res, err = _send('project_write_file',
                         {'root': _ROOT_NAME, 'path': 'README.md',
                          'content': '# app\nhello world\nv2\n'}, aid)
        assert err is None and res.get('snapshot'), res
        assert os.path.isfile(res['snapshot'])
        assert f'{os.sep}.tofu{os.sep}file-history{os.sep}' in res['snapshot']
        assert open(res['snapshot'], encoding='utf-8').read() == \
            '# app\nhello world\n'
        assert seed.read_text(encoding='utf-8') == '# app\nhello world\nv2\n'

        # 3) external (IDE/user) edit → freshness refusal
        seed.write_text('# app\nhello world\nv2\nEXTERNALLY EDITED\n',
                        encoding='utf-8')
        res, err = _send('project_write_file',
                         {'root': _ROOT_NAME, 'path': 'README.md',
                          'content': 'stale overwrite\n'}, aid)
        assert (res and 'changed on disk' in res.get('error', '')) \
            or (err and 'changed on disk' in err), (res, err)

        # 4) re-read re-arms → write allowed
        res, err = _send('project_read_files',
                         {'root': _ROOT_NAME, 'path': 'README.md'}, aid)
        assert err is None
        res, err = _send('project_write_file',
                         {'root': _ROOT_NAME, 'path': 'README.md',
                          'content': '# app\nv3\n'}, aid)
        assert err is None, (res, err)
        assert seed.read_text(encoding='utf-8') == '# app\nv3\n'

        # 5) apply_diff over the wire
        res, err = _send('project_apply_diff',
                         {'root': _ROOT_NAME, 'path': 'README.md',
                          'search': 'v3', 'replace': 'v4'}, aid)
        assert err is None and res.get('replacements') == 1, (res, err)
        assert 'v4' in seed.read_text(encoding='utf-8')

        # 6) streamed run_command: final outcome AND deduped stream frames
        marker = f'e2e-marker-{int(time.time())}'
        cmd_id = 'e2e-run-cmd-1'
        res, err = _send('project_run_command',
                         {'root': _ROOT_NAME,
                          'command': f'echo {marker} && cat README.md',
                          'timeout': 60}, aid, timeout=60, cmd_id=cmd_id)
        assert err is None, (res, err)
        assert res.get('exit_code') == 0 and marker in res.get('stdout', '')
        assert 'v4' in res.get('stdout', '')  # cwd really is the share root
        # Stream reassembly: resolve_streams + get_command_stream (seq-dedup).
        deadline = time.time() + 10
        stream = None
        while time.time() < deadline:
            stream = db.get_command_stream(cmd_id)
            if stream and stream['done']:
                break
            time.sleep(0.2)
        assert stream and stream['done'], stream
        assert marker in stream['stdout']

        # 7) path escape refused over the wire (agent-side jail)
        res, err = _send('project_write_file',
                         {'root': _ROOT_NAME, 'path': '../evil.txt',
                          'content': 'escaped'}, aid)
        refused = (res and res.get('error')) or (err or '')
        assert 'escape' in refused, (res, err)
        assert not (tmp_path / 'evil.txt').exists()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        agent_log.close()
        # Leave the in-process bridge as clean as we found it — sibling suites
        # in the same pytest session share this registry.
        with db.command_queue_lock:
            for aid in [a for a, v in db._agents.items()
                        if _ROOT_NAME in [r.get('name')
                                          for r in v.get('share_roots') or []]]:
                db._agents.pop(aid, None)
