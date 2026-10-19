"""Owned Desktop Agent fixture using the real polling loop and bridge."""
import json
import threading
import time
from lib.desktop_wire import protocol_version
from types import SimpleNamespace

import pytest

from lib.desktop import bridge
from lib.desktop_agent import _run


@pytest.fixture
def desktop_paper_device(tmp_path, monkeypatch, request):
    options = getattr(request, "param", {})
    owner = str(options.get("owner_user_id", 7))
    for name in ('command_queue', '_streams', '_agents'):
        monkeypatch.setattr(bridge, name, {})
    root = tmp_path / 'device-project'
    paper = root / 'paper'
    paper.mkdir(parents=True)
    (paper / 'main.tex').write_text('\\documentclass{article}\n\\begin{document}\n'
                                  'A version from the desktop.\n\\end{document}\n')
    config = tmp_path / 'agent.json'
    config.write_text(json.dumps({'share_roots': [{'name': 'project', 'path': str(root)}]}))
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(config))
    monkeypatch.setenv('TOFU_REMOTE_WORKTREE', '1')
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'device')
    from lib.desktop_agent import _paper, _project
    monkeypatch.setattr(_project, '_freshness', {})
    temporary = tmp_path / 'device-temporary'
    temporary.mkdir()
    class OwnedTemporaryWorker(_paper.PaperBuildWorker):
        def __init__(self, stopped, connected):
            super().__init__(stopped, connected, temporary_parent=temporary)
    monkeypatch.setattr(_paper, 'PaperBuildWorker', OwnedTemporaryWorker)
    ready, stop = threading.Event(), threading.Event()
    commands, sent_frames = [], []
    original_post = _run.requests.Session.post
    def post(session, url, **kwargs):
        if not url.startswith('https://isolated-device.test/'):
            return original_post(session, url, **kwargs)
        body = kwargs['json']
        assert body['agent']['capabilities']['paper_build'] == 1
        bridge.register_agent('device', body['agent'], user_id=owner)
        bridge.resolve_streams(body['streams'], agent_id='device', user_id=owner)
        bridge.resolve_results(body['results'], agent_id='device', user_id=owner)
        sent_frames.extend(body['streams'])
        pending = bridge.take_pending_commands(agent_id='device', user_id=owner)
        commands.extend(pending)
        ready.set()
        return SimpleNamespace(status_code=200, json=lambda: {'commands': pending, 'protocol_version': protocol_version(), 'server_time': time.time()})
    monkeypatch.setattr(_run.requests.Session, 'post', post)
    thread = threading.Thread(target=_run.run_agent,
        args=('https://isolated-device.test', {'allow_exec': True, 'allow_write': bool(options.get('allow_write'))}),
        kwargs={'poll_interval': .01, 'stop_event': stop}, daemon=True)
    thread.start()
    assert ready.wait(3)
    try:
        yield SimpleNamespace(root=root, paper=paper, stop=stop, commands=commands,
                              frames=sent_frames, temporary=temporary)
    finally:
        stop.set()
        thread.join(timeout=7)
        assert not thread.is_alive()
        assert list(temporary.iterdir()) == []

