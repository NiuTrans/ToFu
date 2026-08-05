#!/usr/bin/env python3
"""E2E: one full Studio-mode task iteration over the FRONTEND'S OWN wire
sequence, plus restart self-repair driven by two REAL app instances sharing
one SQLite DB file.

Coverage boundary (owner-mandated)
----------------------------------
These tests pin the BACKEND contract only: ``POST /api/v1/chat/send`` →
``GET /api/chat/stream/<id>`` → tool rounds → ``done`` frame → DB persistence
→ boot-time recovery/re-dispatch. The frontend JS wiring (index.html,
sse_pipeline.js, chatMode dial) is NOT covered here — that is Layer 3 (real
browser), which is deliberately out of scope until the Playwright runtime in
this environment is repaired. What IS proven here:

  Layer 1 (``test_studio_full_iteration_from_frontend_wire``)
    A complete Studio-mode iteration with a REAL tool round: the mock LLM
    requests ``write_file`` and the orchestrator executes it against a tmp
    project — the file must exist ON DISK with the exact bytes (SSE frames
    alone would not prove Studio's project-tool substitution works).

  Layer 2 (``test_restart_self_repair_two_app_instances``)
    Instance A (this pytest process, real app via test_client) starts a
    STUDIO turn (chatMode='studio' + tmp projectPath) whose mock stream
    DRIPS mid-generation (deltas >5s apart — real activity that defeats
    stall detection while tripping the delta-driven 5s checkpoint
    throttle); once a ``status='running'`` checkpoint with content is
    durably in task_results, instance A is "SIGKILLed" — the in-memory
    task is dropped from the registry with the DB corpse left behind,
    exactly what an OS kill leaves. Instance B is then launched as a
    SEPARATE OS PROCESS (fresh ``TOFU_DATA_DIR``, same ``TOFU_DB_PATH``
    file) that runs the REAL startup path — ``server._init_database()``
    (the very call ``_startup`` makes) plus the same
    ``run_deferred_boot_dispatch`` consumption ``_serve`` performs — with
    ``TOFU_BOOT_AUTO_DISPATCH=1``. It must: mark the corpse interrupted,
    tag the tail ``killed``, re-dispatch the turn WITH THE STUDIO TOOL
    SURFACE INTACT (carrier config keeps chatMode='studio' + projectPath),
    and the recovered turn must REALLY execute write_file against the tmp
    project (recovered_marker.txt on disk) — 'Studio repaired itself',
    not 'a blob of text came back'. Finally, instance B reconnects to the
    SSE the way a frontend does after a restart (``Last-Event-ID: 0``
    warm replay) and the replayed ``done`` frame's ``committedMessage``
    must be BYTE-IDENTICAL to the conversations.messages tail — the
    recovered turn lands exactly like a live-completed one.

  NEUTER proofs (owner gate — every assertion must have causal bite):
    * ``test_NEUTER_studio_iteration_neutered_tool_execution`` — strip the
      write_file execution handler; the SAME flow must leave NO file on disk
      (Layer 1's disk assertion goes red under this mutation).
    * ``test_NEUTER_restart_without_recovery_leaves_corpse`` — boot instance
      B with the recovery path amputated (env switch in the boot script);
      NOTHING may be re-dispatched and the corpse must stay ``running``
      (Layer 2's completion assertions go red under this mutation).

Note: the instance-A "kill" leaves one zombie orchestrator thread parked on
the hung mock stream; it is released only AFTER all assertions (mock.close())
so it can never clobber the state under test, and it writes only to the
unique test-conv (session-purged, ``test-conv%`` LIKE pattern).

Auto-heal is OPT-IN — the default is display-only + MANUAL resume
------------------------------------------------------------------
``TOFU_BOOT_AUTO_DISPATCH`` is DEFAULT OFF (owner-mandated, see
lib/tasks_pkg/manager/_recovery.py): an out-of-the-box restart only marks
the corpse interrupted + tags the tail ``killed`` for the sidebar — it
does NOT re-dispatch anything. The real user's repair path on a default
deploy is the MANUAL Continue button. Layer 2 pins the OPT-IN auto path;
Layer 2b (``test_restart_default_mode_manual_resume``) pins the DEFAULT
path: display-only boot facts, then the frontend's REAL manual-resume
wire sequence (POST /api/v1/chat/continue → taskId, or fallback → pop +
allowTruncate PUT + resend) completing the Studio turn with the tool
surface intact. Read "it repairs itself after a restart" honestly: AUTO
only when explicitly enabled; by DEFAULT it repairs itself WHEN THE USER
CLICKS CONTINUE — and neither path may degrade Studio to plain chat.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import pytest

# ci_serial: these boot TWO real app instances sharing one SQLite file and
# poll for running checkpoints with 120s ceilings — under the CI parallel
# lane's CPU contention the polls starve and the suite 120s-times-out (six
# reds on e5adbfa's unit leg) while passing in 48s on an uncontended box.
# The serial lane runs it alone with a 600s timeout.
pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))

_WRITE_PATH = 'hello_studio.txt'
_WRITE_CONTENT = 'hello from studio e2e\nwritten by the real write_file handler\n'
_FINAL_TEXT = 'Studio iteration complete — the file is on disk.'
_RECOVERED_TEXT = 'Recovered answer after restart — the killed turn healed.'
_RECOVER_WRITE_PATH = 'recovered_marker.txt'
_RECOVER_WRITE_CONTENT = 'written by the RECOVERED studio turn — project tools survived\n'


# ═══════════════════════════════════════════════════════════════════════
#  Mock LLM harness (purpose-built: scripted tool call / mid-stream hang)
# ═══════════════════════════════════════════════════════════════════════

def _sse(obj) -> str:
    return f'data: {json.dumps(obj)}\n\n'


def _chunk(model, delta, finish_reason=None, usage=None):
    c = {'id': 'chatcmpl-e2e', 'object': 'chat.completion.chunk',
         'created': int(time.time()), 'model': model,
         'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish_reason}]}
    if usage:
        c['usage'] = usage
    return c


def _free_port() -> int:
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _MockLLM:
    """Tiny OpenAI-compatible mock with two scripted scenarios.

    Built on stdlib ``http.server`` — NOT Flask — because conftest installs a
    Flask→Quart import shim for collection, so ``from flask import Flask``
    resolves into quart's namespace inside pytest.

    scenario='studio_write'
      request without a tool result  → stream a ``write_file`` tool call
      request WITH a tool result     → stream the final text answer

    scenario='drip_then_freeze'
      stream request #1              → drip content deltas ~6s apart (each
                                       drip is healthy activity — no stall
                                       detector fires — and >5s apart so the
                                       orchestrator's delta-driven 5s
                                       checkpoint throttle writes a
                                       status='running' row), then PARK on
                                       ``hang_event`` (the mid-stream kill)
      stream request #2+             → stream the recovered answer

    scenario='studio_recover_write'
      stream request #1              → same drip-then-park (the KILLED studio
                                       turn)
      stream request #2              → a REAL ``write_file`` tool call for
                                       recovered_marker.txt (the re-dispatched
                                       carrier — proves the Studio tool
                                       surface survived the restart)
      stream request #3+             → stream the recovered answer

    Non-stream requests (the memory-relevance filter and other helpers use
    the non-streaming chat() path) get an instant, well-formed JSON
    completion — feeding them SSE would stall them on the park branch (they
    read to connection close) and spew invalid-JSON noise.
    """

    def __init__(self, scenario: str):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.scenario = scenario
        self.history: list[dict] = []
        self.hang_event = threading.Event()
        self._stream_count = {'n': 0}
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                # Kill Nagle on the mock socket: heartbeat bytes must leave
                # the instant they are written (see _HEARTBEAT's comment).
                try:
                    self.connection.setsockopt(
                        socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    pass

            def log_message(self, *a):  # silence per-request noise
                pass

            def do_GET(self):
                if self.path == '/health':
                    payload = json.dumps(
                        {'ok': True, 'requests': len(outer.history)}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_error(404)

            def do_POST(self):
                if self.path != '/v1/chat/completions':
                    self.send_error(404)
                    return
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length) or b'{}')
                outer.history.append(body)
                msgs = body.get('messages') or []
                model = body.get('model') or 'mock-studio-1'
                if not body.get('stream'):
                    # Non-stream helper call (e.g. memory-relevance filter):
                    # instant well-formed JSON completion, never the hang.
                    payload = json.dumps({
                        'id': 'chatcmpl-e2e', 'object': 'chat.completion',
                        'created': int(time.time()), 'model': model,
                        'choices': [{'index': 0, 'message': {
                            'role': 'assistant', 'content': 'OK'},
                            'finish_reason': 'stop'}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
                                  'total_tokens': 2}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                outer._stream_count['n'] += 1
                n = outer._stream_count['n']
                if outer.scenario == 'studio_write':
                    has_tool_result = any(m.get('role') == 'tool' for m in msgs)
                    stream = (outer._text_stream(model, _FINAL_TEXT)
                              if has_tool_result
                              else outer._tool_call_stream(model))
                elif outer.scenario == 'studio_recover_write':
                    if n == 1:
                        stream = outer._drip_stream(model)
                    elif n == 2:
                        stream = outer._tool_call_stream(
                            model, path=_RECOVER_WRITE_PATH,
                            content=_RECOVER_WRITE_CONTENT,
                            call_id='call_e2e_recover')
                    else:
                        stream = outer._text_stream(model, _RECOVERED_TEXT)
                elif n == 1:
                    stream = outer._drip_stream(model)
                else:
                    stream = outer._text_stream(model, _RECOVERED_TEXT)
                # HTTP/1.0 close-delimited SSE body (connection close = end).
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                try:
                    for chunk in stream:
                        self.wfile.write(chunk.encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.port = _free_port()
        self.base_url = f'http://127.0.0.1:{self.port}/v1'
        self._server = ThreadingHTTPServer(('127.0.0.1', self.port), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True, name='mock-llm-e2e')
        self._thread.start()
        # Wait until the server accepts connections.
        import urllib.request
        deadline = time.time() + 20
        while True:
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{self.port}/health',
                                       timeout=1)
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError('mock LLM server did not come up')
                time.sleep(0.1)

    # ── stream builders ──
    def _tool_call_stream(self, model, path=_WRITE_PATH,
                          content=_WRITE_CONTENT, call_id='call_e2e_write'):
        args = json.dumps({'path': path, 'content': content})
        msg = {'role': 'assistant', 'content': None,
               'tool_calls': [{'id': call_id, 'index': 0,
                               'type': 'function',
                               'function': {'name': 'write_file',
                                            'arguments': args}}]}
        yield _sse(_chunk(model, msg, finish_reason='tool_calls',
                          usage={'prompt_tokens': 10, 'completion_tokens': 5,
                                 'total_tokens': 15}))
        yield 'data: [DONE]\n\n'

    def _text_stream(self, model, text):
        words = text.split(' ')
        for i, w in enumerate(words):
            delta = {'content': w + (' ' if i < len(words) - 1 else '')}
            if i == 0:
                delta['role'] = 'assistant'
            yield _sse(_chunk(model, delta))
        yield _sse(_chunk(model, {}, finish_reason='stop',
                          usage={'prompt_tokens': 10,
                                 'completion_tokens': len(words),
                                 'total_tokens': 10 + len(words)}))
        yield 'data: [DONE]\n\n'

    _DRIP_WORDS = ('Partial answer — this turn is being generated ',
                   'slowly enough that the 5s crash-checkpoint throttle ',
                   'fires between drips, leaving a durable running row ')
    _DRIP_GAP_S = 6.0
    # Heartbeat: an SSE COMMENT (parser-invisible — no content, no
    # checkpoint side effects) padded to ~4 KiB. The padding is load-
    # bearing: tiny writes get Nagle-coalesced and arrive in ~6s bursts,
    # so the dispatcher's ~5s urllib3 read_timeout fires mid-drip →
    # "premature close" → the zombie's turn auto-retry EATS the scripted
    # tool_call stream slot (the full-suite flake, proven via the RawSSE
    # anomaly ring + a raw-requests repro). 4 KiB forces an immediate
    # segment on every heartbeat, so the client's read timeout always
    # resets while the zombie stays deterministically inert.
    _HEARTBEAT = ': ' + ('hb' * 2048) + '\n\n'

    def _drip_stream(self, model):
        # Content drips land >5s apart so the orchestrator's delta-driven
        # ``checkpoint_task_partial`` throttle writes a status='running'
        # task_results row mid-stream — the SIGKILL corpse. SSE comment
        # heartbeats every ~2s fill the gaps (and the post-drip park) so
        # the client READ-TIMEOUT never fires: without them the parked
        # zombie's stream dies "premature close" ~5s after the last drip,
        # the production TURN AUTO-RETRY (correctly) re-fires, and the
        # zombie's retry EATS the scripted stream-#2 tool_call slot before
        # instance B's resume gets there (the full-suite flake — order-
        # dependent, invisible in isolation).
        for i, w in enumerate(self._DRIP_WORDS):
            delta = {'content': w}
            if i == 0:
                delta['role'] = 'assistant'
            yield _sse(_chunk(model, delta))
            if i < len(self._DRIP_WORDS) - 1:
                # ~6s to the next content drip, heartbeat every ~2s.
                for _ in range(3):
                    if self.hang_event.wait(2.0):
                        return
                    yield self._HEARTBEAT
        # The "kill": parked mid-stream — heartbeats keep the socket alive
        # and the zombie deterministically inert (it NEVER makes a second
        # request, so stream #2 always belongs to the recovery under test)
        # until teardown releases it.
        while not self.hang_event.wait(2.0):
            yield self._HEARTBEAT
        yield 'data: [DONE]\n\n'

    def close(self):
        self.hang_event.set()
        try:
            self._server.shutdown()
        except Exception:
            pass
        try:
            self._server.server_close()
        except Exception:
            pass
        self._thread.join(timeout=5)


# ═══════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═══════════════════════════════════════════════════════════════════════

def _write_provider_config(config_dir: str, base_url: str):
    os.makedirs(config_dir, exist_ok=True)
    cfg = {'providers': [{
        'id': 'mock-prov', 'name': 'Mock Provider', 'enabled': True,
        'brand': 'local',  # no-proxy bypass + blank-key tolerance
        'base_url': base_url, 'api_keys': ['test-key'],
        'models': [{'model_id': 'mock-studio-1'}],
    }]}
    with open(os.path.join(config_dir, 'server_config.json'), 'w') as f:
        json.dump(cfg, f)


@contextlib.contextmanager
def _point_app_at_mock(config_dir: str, base_url: str):
    """Re-point this process's dispatcher at the mock provider config.

    ``LLMDispatcher._build_slots`` re-reads ``lib._SERVER_CONFIG_PATH`` from
    disk on every fresh build, so patching the path + resetting the singleton
    swaps the whole LLM surface. Restored (and re-resolved) on exit.
    """
    import lib
    import lib.config_dir
    import lib.llm_dispatch as disp

    _write_provider_config(config_dir, base_url)
    old_path = lib._SERVER_CONFIG_PATH
    old_dir = lib.config_dir.CONFIG_DIR
    lib._SERVER_CONFIG_PATH = os.path.join(config_dir, 'server_config.json')
    lib.config_dir.CONFIG_DIR = config_dir
    lib.reload_config()
    disp.reset_dispatcher()
    try:
        yield
    finally:
        lib._SERVER_CONFIG_PATH = old_path
        lib.config_dir.CONFIG_DIR = old_dir
        lib.reload_config()
        disp.reset_dispatcher()


def _parse_sse_frames(text: str) -> list[dict]:
    events = []
    for block in text.split('\n\n'):
        data = [ln[5:] for ln in block.splitlines() if ln.startswith('data:')]
        if not data:
            continue
        payload = '\n'.join(data).strip()
        if not payload:
            continue
        try:
            events.append(json.loads(payload))
        except ValueError:
            continue
    return events


def _consume_sse(client, task_id: str, timeout: float = 180.0) -> list[dict]:
    """Drive ``GET /api/chat/stream/<id>`` to completion (live path)."""
    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(client.get, f'/api/chat/stream/{task_id}')
    try:
        resp = fut.result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)
    return _parse_sse_frames(resp.get_data(as_text=True))


def _send_message(client, conv_id: str, text: str, *, config: dict,
                  settings: dict) -> dict:
    resp = client.post('/api/v1/chat/send', json={
        'convId': conv_id,
        'message': {'text': text},
        'config': config,
        'settings': settings,
    })
    assert resp.status_code == 200, f'send failed: {resp.status_code} {resp.get_data(as_text=True)[:400]}'
    body = resp.get_json()
    assert body.get('taskId'), f'send returned no taskId: {body}'
    return body


def _conv_tail(conv_id: str):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    assert row, f'conversation {conv_id} missing'
    return json.loads(row['messages'] or '[]'), json.loads(row['settings'] or '{}')


def _wait_running_checkpoint(conv_id: str, timeout: float = 120.0):
    """Poll task_results until the live task has a running row WITH content."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    deadline = time.time() + timeout
    db = get_thread_db(DOMAIN_CHAT)
    while time.time() < deadline:
        rows = db.execute(
            'SELECT task_id, status, content FROM task_results WHERE conv_id=?',
            (conv_id,)).fetchall()
        for r in rows:
            if r['status'] == 'running' and (r['content'] or ''):
                return r
        time.sleep(0.5)
    raise AssertionError(
        f'no running checkpoint with content appeared for conv={conv_id[:8]} '
        f'within {timeout:.0f}s (rows={[(r["task_id"][:8], r["status"]) for r in rows]})')


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _assert_tail_matches_committed(tail: dict, committed: dict):
    """BYTE-IDENTITY contract: done.committedMessage == conversations tail.

    ``_committedMsg`` is stamped by ``_sync_result_to_conversation`` with the
    exact dict it wrote (re-SELECT-post-CAS), so identity holds BY
    CONSTRUCTION at sync time — modulo TWO documented async lanes in the
    finalize pipeline (observed live, both hit a plain non-recovered turn
    identically, so they cannot mask a recovery-specific defect):

      1. Post-commit annotations — the commit_round / file-history daemons
         stamp ``_gitSha`` / ``_snapshotId`` onto the persisted message AFTER
         the done event rides out.
      2. Segment-timeline finalization — ``segments`` may still be completing
         when ``_committedMsg`` is snapshotted (a race inside finalize): the
         committed copy can hold a PREFIX of the final text segment. The DB
         tail always carries the finalized timeline. Compared structurally
         below: same count, every non-text field identical, and committed
         text may only ever be a PREFIX of the tail's (monotone finalization,
         never divergence).

    EVERYTHING ELSE — content, thinking, toolRounds, finishReason, model,
    usage, timestamps, msgIds, and every other key — must be byte-identical.
    """
    _POST_COMMIT_ANNOTATIONS = frozenset({'_gitSha', '_snapshotId'})
    t, c = dict(tail), dict(committed)
    extra = set(t) - set(c)
    missing = set(c) - set(t)
    assert not missing, \
        f'committedMessage carries keys the DB tail lacks: {sorted(missing)}'
    assert extra <= _POST_COMMIT_ANNOTATIONS, \
        f'DB tail drifted beyond the async post-commit annotations: {sorted(extra)}'
    for k in extra:
        t.pop(k)
    # Lane 2: segments get the structural comparator; the rest of the message
    # goes through strict recursive byte-identity.
    t_segs, c_segs = t.pop('segments', None), c.pop('segments', None)
    _assert_segments_equivalent(t_segs, c_segs)
    leaf = _first_diff(t, c)
    assert leaf is None, (
        'done.committedMessage is not byte-identical to the DB tail '
        f'(modulo async lanes) — first diff at {leaf[0]}:\n'
        f'  tail     = {leaf[1]!r:.300}\n'
        f'  committed= {leaf[2]!r:.300}')


def _assert_segments_equivalent(tail_segs, committed_segs):
    """Segments: STRICT byte-identity.

    Post-pt_687b87ac the terminal sync re-assembles the timeline before
    stamping, so the done frame's committed segments must equal the DB
    tail's VERBATIM — the historical prefix tolerance was the race this
    epic root-fixed. Kept as a named helper so the failure message stays
    precise.
    """
    assert isinstance(tail_segs, list) and isinstance(committed_segs, list), \
        f'segments missing on one side: tail={type(tail_segs)} committed={type(committed_segs)}'
    leaf = _first_diff(tail_segs, committed_segs, '$.segments')
    assert leaf is None, (
        'segment timeline drift between done.committedMessage and DB tail '
        f'(pt_687b87ac regression) — first diff at {leaf[0]}:\n'
        f'  tail     = {leaf[1]!r:.300}\n'
        f'  committed= {leaf[2]!r:.300}')


def _first_diff(a, b, path='$'):
    """Return (path, a_val, b_val) of the first leaf difference, or None."""
    if type(a) is not type(b):
        return (path, a, b)
    if isinstance(a, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            return (path, sorted(ka - kb), sorted(kb - ka))
        for k in sorted(a):
            r = _first_diff(a[k], b[k], f'{path}.{k}')
            if r:
                return r
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return (path, f'len={len(a)}', f'len={len(b)}')
        for i, (x, y) in enumerate(zip(a, b)):
            r = _first_diff(x, y, f'{path}[{i}]')
            if r:
                return r
        return None
    return None if a == b else (path, a, b)


# ═══════════════════════════════════════════════════════════════════════
#  Layer 1 — full Studio iteration with a real disk side effect
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
def test_studio_full_iteration_from_frontend_wire(flask_client, tmp_path):
    """The frontend's exact wire sequence, Studio tier, REAL write_file round.

    Assertions (owner gate #1): the file the mock asked to write must exist
    in the tmp project with exact bytes — SSE-frame-only assertions would not
    prove Studio's project-tool family actually executes.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    (project / 'existing.py').write_text('print(1)\n')
    mock = _MockLLM(scenario='studio_write')
    conv_id = 'test-conv-studio-' + uuid.uuid4().hex[:12]
    try:
        with _point_app_at_mock(str(tmp_path / 'cfg'), mock.base_url):
            body = _send_message(
                flask_client, conv_id, 'please create the hello file',
                config={'chatMode': 'studio', 'model': 'mock-studio-1',
                        'projectPath': str(project), 'autoApply': True,
                        'searchMode': 'off', 'fetchEnabled': False,
                        'memoryEnabled': False},
                settings={'model': 'mock-studio-1', 'chatMode': 'studio',
                          'projectPath': str(project), 'searchMode': 'off',
                          'fetchEnabled': False, 'memoryEnabled': False})
            events = _consume_sse(flask_client, body['taskId'])

        # ── 1. SSE contract: a live done frame, no error ──
        done = [e for e in events if isinstance(e, dict) and e.get('type') == 'done']
        assert done, f'no done frame; events={[e.get("type") for e in events]}'
        done = done[-1]
        assert not done.get('error'), f'done carries error: {done.get("error")}'
        assert done.get('finishReason') == 'stop', done.get('finishReason')

        # ── 2. THE DISK SIDE EFFECT — Studio's raison d'être ──
        target = project / _WRITE_PATH
        assert target.is_file(), \
            f'{_WRITE_PATH} was not created in the project (tool never executed?)'
        assert target.read_text() == _WRITE_CONTENT

        # ── 3. The mock saw the Studio tool surface (write_file offered) ──
        # history[0] may be a non-stream helper call (memory filter) — pick
        # the streaming requests that carry the tool surface.
        with_tools = [h for h in mock.history if h.get('tools')]
        assert with_tools, \
            f'no request carried tools: {[sorted(h.keys()) for h in mock.history]}'
        names = [t.get('function', {}).get('name')
                 for t in (with_tools[0].get('tools') or [])]
        assert 'write_file' in names, f'write_file not offered: {names}'
        # The first tool-carrying request got the tool_call response (it had
        # no tool result yet); a LATER one must carry the write result back.
        assert not any(m.get('role') == 'tool'
                       for m in (with_tools[0].get('messages') or []))
        assert any(m.get('role') == 'tool'
                   and m.get('tool_call_id') == 'call_e2e_write'
                   for h in with_tools[1:]
                   for m in (h.get('messages') or [])), \
            'no later LLM call carried the write_file tool result — the round never closed'

        # ── 4. DB truth: done.committedMessage == conversations tail (bytes) ──
        committed = done.get('committedMessage')
        assert committed, 'done frame carries no committedMessage'
        messages, _settings = _conv_tail(conv_id)
        tail = messages[-1]
        assert tail.get('role') == 'assistant'
        _assert_tail_matches_committed(tail, committed)
        assert tail.get('finishReason') == 'stop'
        assert _FINAL_TEXT in (tail.get('content') or '')
        # The tool round landed in the persisted assistant message.
        assert any((r.get('toolName') or r.get('name')) == 'write_file'
                   for r in (tail.get('toolRounds') or [])), \
            'persisted assistant message lacks the write_file tool round'

        # ── 5. task_results agrees ──
        from lib.database import DOMAIN_CHAT, get_thread_db
        rows = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT status FROM task_results WHERE task_id=?',
            (body['taskId'],)).fetchall()
        assert rows and rows[0]['status'] == 'done', rows
    finally:
        mock.close()


@pytest.mark.unit
def test_NEUTER_studio_iteration_neutered_tool_execution(flask_client, tmp_path,
                                                         monkeypatch):
    """NEUTER proof for Layer 1 (owner gate #3).

    Strip the write_file EXECUTION handler (the tool round still happens —
    the model still asks for the write — but the executor is amputated).
    Under this mutation Layer 1's disk assertion MUST go red: no file may
    appear. If this test ever goes green while the mutation is active, the
    Layer-1 assertions have no causal power.
    """
    import lib.project_mod.tools as pmt

    def _neutered_write_file(fn_args, base_path, conv_id, task_id, kwargs):
        return 'NEUTERED: write_file handler removed by mutation test'

    monkeypatch.setitem(pmt._EXEC_HANDLERS, 'write_file', _neutered_write_file)

    project = tmp_path / 'proj'
    project.mkdir()
    mock = _MockLLM(scenario='studio_write')
    conv_id = 'test-conv-studio-nc-' + uuid.uuid4().hex[:12]
    try:
        with _point_app_at_mock(str(tmp_path / 'cfg'), mock.base_url):
            body = _send_message(
                flask_client, conv_id, 'please create the hello file',
                config={'chatMode': 'studio', 'model': 'mock-studio-1',
                        'projectPath': str(project), 'autoApply': True,
                        'searchMode': 'off', 'fetchEnabled': False,
                        'memoryEnabled': False},
                settings={'model': 'mock-studio-1', 'chatMode': 'studio',
                          'projectPath': str(project), 'searchMode': 'off',
                          'fetchEnabled': False, 'memoryEnabled': False})
            events = _consume_sse(flask_client, body['taskId'])

        # Sanity: the model DID request the write (the mutation didn't just
        # skip the tool round entirely).
        assert any(m.get('role') == 'tool'
                   and m.get('tool_call_id') == 'call_e2e_write'
                   for h in mock.history
                   for m in (h.get('messages') or [])), \
            'neutered run: the tool round did not even happen'

        # THE NEUTERED OUTCOME: Layer 1's central assertion goes red here.
        assert not (project / _WRITE_PATH).exists(), \
            'NEUTER failed: file was written despite the amputated handler'
        # The turn still completes (the tool result is the neuter string),
        # so this failure is attributable to the execution path alone.
        done = [e for e in events if isinstance(e, dict) and e.get('type') == 'done']
        assert done, 'neutered run produced no done frame at all'
    finally:
        mock.close()


# ═══════════════════════════════════════════════════════════════════════
#  Layer 2 — restart self-repair: two REAL app instances, one DB file
# ═══════════════════════════════════════════════════════════════════════

# Boot script for instance B. Runs the REAL startup path in a separate OS
# process: ``server._init_database()`` (the literal call server._startup
# makes) + ``run_deferred_boot_dispatch`` (the literal consumption _serve
# performs). Dumps a JSON report for the parent pytest process to assert on.
_BOOT_SCRIPT = r'''
import asyncio, json, os, sys, time

result_path, conv_id = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.getcwd())
out = {'ok': False, 'conv_id': conv_id}

_SKIP = os.environ.get('TOFU_E2E_SKIP_RECOVERY') == '1'
_MANUAL = os.environ.get('TOFU_E2E_MANUAL') == '1'
_NEUTER_WRITE = os.environ.get('TOFU_E2E_NEUTER_WRITE') == '1'
_PROJECT_PATH = os.environ.get('TOFU_E2E_PROJECT_PATH') or ''

try:
    import server
    from server import app
    from lib.database import DOMAIN_CHAT, get_thread_db, init_db

    def _conv_row(db):
        return db.execute(
            'SELECT messages, settings, title FROM conversations'
            ' WHERE id=? AND user_id=1', (conv_id,)).fetchone()

    if _SKIP:
        # ── NEUTER MODE: amputate the recovery path entirely ──
        # Only bring the DB up (init_db is the non-recovery part of
        # _init_database). No shutdown classification, no stale-task sweep,
        # no deferred dispatch — exactly "the recovery code was deleted".
        init_db()
        out['neutered'] = True
        out['descriptor'] = None
        # Give any hypothetical re-dispatch a fair chance to fire.
        time.sleep(10)
    else:
        # ── REAL STARTUP PATH ──
        # The very two calls the server makes at boot/serving time.
        async def _boot():
            async with app.app_context():
                server._init_database()
        asyncio.run(_boot())
        out['descriptor'] = server._DEFERRED_BOOT_DISPATCH
        from lib.tasks_pkg import run_deferred_boot_dispatch
        run_deferred_boot_dispatch(server._DEFERRED_BOOT_DISPATCH)
        from lib.tasks_pkg.manager import tasks, tasks_lock

        if _MANUAL:
            # ══ DEFAULT-DEPLOY MODE (TOFU_BOOT_AUTO_DISPATCH unset) ══
            # The boot did its display-only half; NOTHING may auto-dispatch.
            with tasks_lock:
                out['auto_carrier_found'] = any(
                    t.get('convId') == conv_id for t in tasks.values())
            db0 = get_thread_db(DOMAIN_CHAT)
            row0 = _conv_row(db0)
            if row0:
                out['conv_messages_before'] = row0['messages']
                out['conv_settings_before'] = row0['settings']
            if _NEUTER_WRITE:
                import lib.project_mod.tools as pmt
                pmt._EXEC_HANDLERS['write_file'] = (
                    lambda fn_args, base_path, cid, tid2, kwargs:
                        'NEUTERED: write_file handler removed by mutation test')
                out['write_neutered'] = True

            # ── The frontend's REAL manual-resume wire sequence ──
            # continueAssistant(): POST /api/v1/chat/continue → taskId, or
            # {fallback:'regenerate'} → pop tail + allowTruncate PUT + resend.
            _cfg = {'chatMode': 'studio', 'model': 'mock-studio-1',
                    'projectPath': _PROJECT_PATH, 'autoApply': True,
                    'searchMode': 'off', 'fetchEnabled': False,
                    'memoryEnabled': False}
            _settings = {'model': 'mock-studio-1', 'chatMode': 'studio',
                         'projectPath': _PROJECT_PATH, 'autoApply': True,
                         'searchMode': 'off', 'fetchEnabled': False,
                         'memoryEnabled': False}

            async def _manual_resume():
                async with app.test_client() as c:
                    resp = await c.post('/api/v1/chat/continue',
                                        json={'convId': conv_id,
                                              'config': _cfg})
                    data = await resp.get_json()
                    out['continue_http'] = resp.status_code
                    out['continue_data'] = data
                    if data.get('fallback') == 'regenerate':
                        out['resume_via'] = 'regenerate-resend'
                        msgs = json.loads(row0['messages'] or '[]')
                        last_user = next(
                            (m for m in reversed(msgs)
                             if m.get('role') == 'user'), {})
                        put = await c.put(
                            '/api/v1/conversations/' + conv_id,
                            json={'title': row0['title'],
                                  'messages': msgs[:-1],
                                  'settings': json.loads(
                                      row0['settings'] or '{}'),
                                  'allowTruncate': True})
                        out['pop_put_http'] = put.status_code
                        resp2 = await c.post('/api/v1/chat/send', json={
                            'convId': conv_id,
                            'message': {'text': last_user.get('content') or ''},
                            'config': _cfg, 'settings': _settings})
                        d2 = await resp2.get_json()
                        out['resend_http'] = resp2.status_code
                        out['resend_data'] = d2
                        return d2.get('taskId')
                    out['resume_via'] = 'continue'
                    return data.get('taskId')

            tid = asyncio.run(_manual_resume())
            out['resume_task_id'] = tid
            if tid:
                async def _grab(t):
                    async with app.test_client() as c:
                        r = await c.get('/api/chat/stream/' + t)
                        return await r.get_data(as_text=True)
                try:
                    out['sse_raw'] = asyncio.run(_grab(tid))
                except Exception as e:
                    out['sse_error'] = repr(e)
                with tasks_lock:
                    rt = tasks.get(tid)
                if rt is not None:
                    _rc = rt.get('config') or {}
                    out['resume_chatMode'] = _rc.get('chatMode')
                    out['resume_projectPath'] = _rc.get('projectPath')
        else:
            # ══ OPT-IN AUTO MODE (TOFU_BOOT_AUTO_DISPATCH=1) ══
            # Wait for the re-dispatched killed-recovery carrier to finish.
            carrier = None
            deadline = time.time() + 240
            while time.time() < deadline:
                with tasks_lock:
                    cands = [t for t in tasks.values()
                             if t.get('convId') == conv_id
                             and t.get('_killed_recovery')]
                if cands:
                    carrier = cands[0]
                    if carrier.get('status') in ('done', 'error', 'aborted'):
                        break
                time.sleep(0.25)
            if carrier is not None:
                out['carrier_id'] = carrier['id']
                out['carrier_status'] = carrier.get('status')
                out['carrier_finish'] = carrier.get('finishReason')
                _cfg = carrier.get('config') or {}
                out['carrier_chatMode'] = _cfg.get('chatMode')
                out['carrier_projectPath'] = _cfg.get('projectPath')

            # Reconnect to the stream the way a frontend does after a
            # restart: Last-Event-ID warm replay — this replays the REAL
            # done event (the synthesized late-done carries meta only).
            if carrier is not None:
                async def _grab(tid):
                    async with app.test_client() as c:
                        resp = await c.get('/api/chat/stream/' + tid,
                                           headers={'Last-Event-ID': '0'})
                        return await resp.get_data(as_text=True)
                try:
                    out['sse_raw'] = asyncio.run(_grab(carrier['id']))
                except Exception as e:
                    out['sse_error'] = repr(e)

    # ── DB report (all modes) ──
    db = get_thread_db(DOMAIN_CHAT)
    row = _conv_row(db)
    if row:
        out['conv_messages'] = row['messages']
        out['conv_settings'] = row['settings']
    out['task_rows'] = [
        [r['task_id'], r['status'], len(r['content'] or '')]
        for r in db.execute(
            'SELECT task_id, status, content FROM task_results WHERE conv_id=?',
            (conv_id,)).fetchall()]
    out['ok'] = True
except Exception as e:
    import traceback
    out['fatal'] = repr(e)
    out['traceback'] = traceback.format_exc()

with open(result_path, 'w') as f:
    json.dump(out, f)
print('INSTANCE-B-DONE ok=%s' % out['ok'])
'''


def _launch_instance_b(tmp_path, conv_id: str, *, mock_url: str,
                       mode: str = 'auto', neuter_write: bool = False,
                       project_path: str = '') -> dict:
    """Launch instance B as a separate OS process sharing the session DB.

    mode='auto'   → TOFU_BOOT_AUTO_DISPATCH=1 (opt-in auto self-repair)
    mode='manual' → DEFAULT deploy (no dispatch env): display-only boot,
                    then the boot script drives the frontend's REAL
                    manual-resume wire sequence (continue → fallback-resend)
    mode='skip'   → NEUTER: the recovery path is amputated entirely
    """
    import lib.database._core as dbc

    assert mode in ('auto', 'manual', 'skip'), mode
    tag = {'auto': 'instanceB', 'manual': 'instanceB-manual',
           'skip': 'instanceB-nc'}[mode]
    if neuter_write:
        tag += '-nw'
    data_dir = tmp_path / tag
    cfg_dir = data_dir / 'data' / 'config'
    cfg_dir.mkdir(parents=True)
    _write_provider_config(str(cfg_dir), mock_url)
    # Plant the shutdown marker instance A left behind: armed ('running'),
    # never marked clean — the SIGKILL signature instance B classifies as
    # 'unclean', which is what tags the recovered tail 'killed'.
    (data_dir / 'data').mkdir(exist_ok=True)
    with open(data_dir / 'data' / '.server_shutdown.json', 'w') as f:
        json.dump({'state': 'running', 'pid': 999999, 'host': 'instance-A',
                   'boot_ts': time.time(), 'reason': 'boot'}, f)

    result_path = tmp_path / (tag + '_result.json')
    env = os.environ.copy()
    env.update({
        'TOFU_DB_BACKEND': 'sqlite',
        'TOFU_DB_PATH': dbc.DB_PATH,           # THE SHARED DB FILE
        'TOFU_DATA_DIR': str(data_dir),        # fresh marker/config root
        'TOFU_DISABLE_SCHEDULER': '1',
        'TOFU_MLOCK': '0',
        'TRADING_ENABLED': '0',
        'PPTX_TRANSLATE_ENABLED': '0',
        'LLM_API_KEYS': 'test-key',
        'LLM_API_KEY': 'test-key',
        'NO_PROXY': '127.0.0.1,localhost',
        'no_proxy': '127.0.0.1,localhost',
    })
    # conftest sets TOFU_BOOT_AUTO_DISPATCH=1 SESSION-WIDE in this process —
    # os.environ.copy() would leak the opt-in switch into every mode. Strip
    # it first; only 'auto' re-adds it. (First-run failure of Layer 2b was
    # exactly this leak: default mode booted with auto-dispatch ON.)
    env.pop('TOFU_BOOT_AUTO_DISPATCH', None)
    if mode == 'auto':
        env['TOFU_BOOT_AUTO_DISPATCH'] = '1'   # the OPT-IN switch
    elif mode == 'manual':
        env['TOFU_E2E_MANUAL'] = '1'           # default deploy: NO auto
        env['TOFU_E2E_PROJECT_PATH'] = project_path
        if neuter_write:
            env['TOFU_E2E_NEUTER_WRITE'] = '1'
    else:
        env['TOFU_E2E_SKIP_RECOVERY'] = '1'
    proc = subprocess.run(
        [sys.executable, '-c', _BOOT_SCRIPT, str(result_path), conv_id],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=420)
    assert os.path.isfile(result_path), (
        f'instance B produced no result file.\nrc={proc.returncode}\n'
        f'stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}')
    result = json.loads(open(result_path).read())
    assert result.get('ok'), (
        f'instance B boot failed: {result.get("fatal")}\n'
        f'{result.get("traceback", "")[-2000:]}\n'
        f'stderr:\n{proc.stderr[-2000:]}')
    return result


def _send_and_kill_studio_turn(client, conv_id: str, project_dir,
                               cfg_dir: str, mock_url: str) -> str:
    """Instance A: run a Studio turn until its running checkpoint is
    durable, then SIGKILL it (registry drop, DB corpse untouched).

    The KILLED turn is itself a Studio turn bound to the project — the
    SETTINGS carry the tier so whatever resumes it later (auto carrier OR
    manual continue) resolves chatMode/projectPath/autoApply from
    conv_settings (both spawn paths pass config=None / minimal overrides).
    Returns the killed task_id.
    """
    with _point_app_at_mock(cfg_dir, mock_url):
        body = _send_message(
            client, conv_id, 'answer me please',
            config={'chatMode': 'studio', 'model': 'mock-studio-1',
                    'projectPath': str(project_dir), 'autoApply': True,
                    'searchMode': 'off', 'fetchEnabled': False,
                    'memoryEnabled': False},
            settings={'model': 'mock-studio-1', 'chatMode': 'studio',
                      'projectPath': str(project_dir), 'autoApply': True,
                      'searchMode': 'off', 'fetchEnabled': False,
                      'memoryEnabled': False})
        task_id = body['taskId']
        # Wait until the mid-stream checkpoint is durable, then SIGKILL
        # instance A: drop the in-memory task, DB corpse untouched.
        _wait_running_checkpoint(conv_id)
        from lib.tasks_pkg.manager import tasks, tasks_lock
        with tasks_lock:
            assert task_id in tasks, 'task vanished before the kill'
            tasks.pop(task_id, None)
        # Sanity: the corpse is exactly what an OS kill leaves behind.
        from lib.database import DOMAIN_CHAT, get_thread_db
        corpse = get_thread_db(DOMAIN_CHAT).execute(
            'SELECT status FROM task_results WHERE task_id=?',
            (task_id,)).fetchone()
        assert corpse['status'] == 'running', corpse['status']
    return task_id


@pytest.mark.unit
def test_restart_self_repair_two_app_instances(flask_client, tmp_path):
    """Instance A killed mid-Studio-stream → B's real boot repairs the turn.

    Assertion chain (owner gate #2): two real app instances share one
    TOFU_DB_PATH file; A leaves a status='running' corpse (no graceful
    close); B walks the real startup path. The KILLED turn is itself a
    Studio turn bound to a tmp project, so the chain also pins STUDIO
    SURVIVAL (owner's gap callout — a chat-mode recovery can never catch
    'healed but degraded to plain chat'): the re-dispatched carrier's
    resolved config must keep chatMode='studio' + projectPath, and the
    recovered turn must REALLY execute a project tool (write_file →
    recovered_marker.txt on disk). Finally the done-frame committedMessage
    must be BYTE-IDENTICAL to the conversations.messages tail — a recovered
    turn lands exactly like a live-completed one.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    (project / 'seed.py').write_text('# seed file\n')
    mock = _MockLLM(scenario='studio_recover_write')
    conv_id = 'test-conv-kill-' + uuid.uuid4().hex[:12]
    try:
        task_id = _send_and_kill_studio_turn(
            flask_client, conv_id, project, str(tmp_path / 'cfg'),
            mock.base_url)

        result = _launch_instance_b(tmp_path, conv_id,
                                    mock_url=mock.base_url,
                                    mode='auto')

        # ── B's real startup path found + re-dispatched the killed turn ──
        descriptor = result.get('descriptor') or {}
        assert conv_id in (descriptor.get('killed_conv_ids') or []), \
            f'conv not in killed_conv_ids: {descriptor}'
        assert result.get('carrier_id'), 'no killed-recovery carrier was spawned'
        assert result.get('carrier_status') == 'done', \
            f"carrier ended as {result.get('carrier_status')}"

        # ── STUDIO SURVIVAL (owner gate): the recovery did NOT degrade the
        # turn to plain chat — the carrier's resolved config kept the tier ──
        assert result.get('carrier_chatMode') == 'studio', \
            f"carrier lost chatMode='studio': {result.get('carrier_chatMode')!r}"
        assert result.get('carrier_projectPath') == str(project), \
            f'carrier lost projectPath: {result.get("carrier_projectPath")!r}'

        # ── THE DECISIVE ASSERTION: the recovered turn really executed a
        # project tool — the marker file exists ON DISK with exact bytes.
        # "It repaired itself in Studio mode" = the project-tool family came
        # back, not just a blob of text.
        marker = project / _RECOVER_WRITE_PATH
        assert marker.is_file(), \
            'recovered turn never executed write_file — Studio self-repair ' \
            'degraded to plain text (project-tool family lost across restart)'
        assert marker.read_text() == _RECOVER_WRITE_CONTENT

        # ── The carrier's LLM requests prove the tool surface end-to-end:
        # write_file was OFFERED on the recovery request and its result came
        # back (the tool round actually closed).
        streams = [h for h in mock.history if h.get('stream')]
        assert len(streams) >= 3, \
            f'expected drip + tool_call + final streams, got {len(streams)}'
        rec_tools = [t.get('function', {}).get('name')
                     for t in (streams[1].get('tools') or [])]
        assert 'write_file' in rec_tools, \
            f'recovered turn lost the Studio tool surface: {rec_tools}'
        assert any(m.get('role') == 'tool'
                   and m.get('tool_call_id') == 'call_e2e_recover'
                   for h in streams[2:] for m in (h.get('messages') or [])), \
            'recovered turn: the write_file tool result never came back'

        # ── The warm-replayed done frame (frontend-reconnect path) ──
        assert result.get('sse_raw'), \
            f"instance B captured no SSE: {result.get('sse_error')}"
        frames = _parse_sse_frames(result['sse_raw'])
        done = [e for e in frames if isinstance(e, dict) and e.get('type') == 'done']
        assert done, f'no done frame in replayed stream: ' \
                     f'{[e.get("type") for e in frames]}'
        done = done[-1]
        assert not done.get('error'), done.get('error')
        assert done.get('finishReason') == 'stop', done.get('finishReason')
        committed = done.get('committedMessage')
        assert committed, 'replayed done carries no committedMessage'

        # ── BYTE-IDENTITY: done.committedMessage == conversations tail ──
        conv_messages = json.loads(result['conv_messages'])
        tail = conv_messages[-1]
        assert tail.get('role') == 'assistant'
        _assert_tail_matches_committed(tail, committed)
        assert tail.get('finishReason') == 'stop', tail.get('finishReason')
        assert _RECOVERED_TEXT in (tail.get('content') or ''), tail.get('content')

        # ── Loop guard consumed exactly one attempt ──
        settings = json.loads(result['conv_settings'] or '{}')
        kr = settings.get('_killedRecovery') or {}
        assert kr.get('attempts') == 1, kr
        # The dead activeTaskId pointer was cleared by the recovery sweep.
        assert settings.get('activeTaskId') in (None, ''), settings.get('activeTaskId')

        # ── The corpse stayed marked interrupted (never clobbered) ──
        rows = {r[0]: r[1] for r in result['task_rows']}
        assert rows.get(task_id) == 'interrupted', rows
        assert rows.get(result['carrier_id']) == 'done', rows

        # ── Independent parent-side re-read (don't just trust B's view) ──
        messages_p, _settings_p = _conv_tail(conv_id)
        _assert_tail_matches_committed(messages_p[-1], committed)
    finally:
        mock.close()


@pytest.mark.unit
def test_NEUTER_restart_without_recovery_leaves_corpse(flask_client, tmp_path):
    """NEUTER proof for Layer 2 (owner gate #3).

    Boot instance B with the recovery path amputated (TOFU_E2E_SKIP_RECOVERY
    → the boot script runs only init_db, never the stale-task sweep nor the
    deferred dispatch). Under this mutation EVERY Layer-2 completion
    assertion goes red: no killed tagging, no carrier, no healed tail — the
    corpse stays status='running' forever. Proves Layer 2's green is caused
    by the real recovery path, not by some incidental mechanism.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    mock = _MockLLM(scenario='studio_recover_write')
    conv_id = 'test-conv-kill-nc-' + uuid.uuid4().hex[:12]
    try:
        task_id = _send_and_kill_studio_turn(
            flask_client, conv_id, project, str(tmp_path / 'cfg'),
            mock.base_url)

        result = _launch_instance_b(tmp_path, conv_id,
                                    mock_url=mock.base_url,
                                    mode='skip')

        # ── The mutation held: nothing was recovered, nothing re-dispatched ──
        assert result.get('neutered') is True
        assert result.get('descriptor') is None
        assert not result.get('carrier_id'), \
            'NEUTER failed: a carrier spawned without the recovery path'

        # The corpse is still status='running' — the recovery sweep is the
        # ONLY thing that ever marks it interrupted.
        rows = {r[0]: r[1] for r in result['task_rows']}
        assert rows.get(task_id) == 'running', \
            f'NEUTER failed: corpse moved without recovery: {rows}'

        # Layer 2's completion assertions, evaluated here, are ALL red:
        conv_messages = json.loads(result['conv_messages'])
        tail = conv_messages[-1]
        assert tail.get('finishReason') != 'stop', \
            'NEUTER failed: the turn completed without the recovery path'
        assert _RECOVERED_TEXT not in (tail.get('content') or '')
        assert not tail.get('interruptedReason'), \
            'NEUTER failed: tail was tagged killed without the recovery sweep'

        # The two STUDIO-SURVIVAL assertions are red too (owner gate #3):
        # no carrier config survives, and no recovered tool side effect lands.
        assert not result.get('carrier_projectPath'), \
            'NEUTER failed: carrier config present without the recovery path'
        assert not (project / _RECOVER_WRITE_PATH).exists(), \
            'NEUTER failed: recovered marker written without the recovery path'
    finally:
        mock.close()


@pytest.mark.unit
def test_restart_default_mode_manual_resume(flask_client, tmp_path):
    """Layer 2b — DEFAULT deploy: display-only boot + MANUAL resume.

    ``TOFU_BOOT_AUTO_DISPATCH`` is DEFAULT OFF (owner-mandated). Out of the
    box a restart does NOT heal anything by itself: the boot marks the
    corpse interrupted + tags the tail ``killed`` for the sidebar, and the
    actual repair happens when the user clicks Continue. This pins the
    default path end-to-end (owner's gap callout — the opt-in auto path
    proved nothing about it):

      1. DISPLAY-ONLY CONTRACT: no auto carrier, corpse ``interrupted``,
         tail tagged ``killed``, no recovery attempts consumed.
      2. The frontend's REAL manual-resume wire sequence —
         ``POST /api/v1/chat/continue`` → ``taskId``, or
         ``{fallback:'regenerate'}`` → pop tail + ``allowTruncate`` PUT +
         resend — completes the Studio turn: resume task keeps
         chatMode='studio' + projectPath, REALLY executes write_file
         (recovered_marker.txt on disk), and the done-frame
         committedMessage is byte-identical to the DB tail.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    (project / 'seed.py').write_text('# seed file\n')
    mock = _MockLLM(scenario='studio_recover_write')
    conv_id = 'test-conv-manual-' + uuid.uuid4().hex[:12]
    try:
        task_id = _send_and_kill_studio_turn(
            flask_client, conv_id, project, str(tmp_path / 'cfg'),
            mock.base_url)

        result = _launch_instance_b(tmp_path, conv_id,
                                    mock_url=mock.base_url,
                                    mode='manual',
                                    project_path=str(project))

        # ── 1. DISPLAY-ONLY CONTRACT (the default boot annotated +
        #       SURFACED the corpse but did NOT dispatch) ──
        # The descriptor is the deferred-dispatch PLAN — recover() returns
        # it whenever the sweep found work, regardless of the gate. The
        # GATE lives in run_deferred_boot_dispatch
        # (_boot_auto_dispatch_enabled, default OFF): display-only = the
        # conv shows up in the plan yet NOTHING was spawned.
        descriptor = result.get('descriptor') or {}
        assert conv_id in (descriptor.get('killed_conv_ids') or []), \
            f'default boot must still SURFACE the killed conv for manual ' \
            f'resume: {descriptor}'
        assert result.get('auto_carrier_found') is False, \
            'default boot auto-dispatched a task — opt-in behaviour leaked'
        rows = {r[0]: r[1] for r in result['task_rows']}
        assert rows.get(task_id) == 'interrupted', rows
        before = json.loads(result['conv_messages_before'])
        killed_msg = before[-1]
        assert killed_msg.get('role') == 'assistant'
        assert killed_msg.get('interruptedReason') == 'killed', \
            f"default boot must tag the corpse tail 'killed': " \
            f'{killed_msg.get("interruptedReason")!r}'
        assert (killed_msg.get('content') or '').startswith('Partial answer')
        before_settings = json.loads(result['conv_settings_before'] or '{}')
        assert before_settings.get('activeTaskId') in (None, ''), \
            before_settings.get('activeTaskId')
        kr = before_settings.get('_killedRecovery') or {}
        assert not kr.get('attempts'), \
            f'auto path consumed attempts in default mode: {kr}'

        # ── 2. MANUAL RESUME (the frontend's real wire sequence) ──
        assert result.get('continue_http') == 200, result.get('continue_data')
        assert result.get('resume_via') in ('continue', 'regenerate-resend')
        if result['resume_via'] == 'regenerate-resend':
            assert result.get('pop_put_http') == 200
            assert result.get('resend_http') == 200, result.get('resend_data')
        resume_tid = result.get('resume_task_id')
        assert resume_tid, \
            f'manual resume produced no task: {result.get("continue_data")}'
        # Studio tier survived the manual resume (not degraded to chat).
        assert result.get('resume_chatMode') == 'studio', \
            f'manual resume lost chatMode: {result.get("resume_chatMode")!r}'
        assert result.get('resume_projectPath') == str(project), \
            f'manual resume lost projectPath: {result.get("resume_projectPath")!r}'

        assert result.get('sse_raw'), result.get('sse_error')
        frames = _parse_sse_frames(result['sse_raw'])
        done = [e for e in frames if isinstance(e, dict) and e.get('type') == 'done']
        assert done, f'no done frame: {[e.get("type") for e in frames]}'
        done = done[-1]
        assert not done.get('error'), done.get('error')
        assert done.get('finishReason') == 'stop', done.get('finishReason')

        # ── THE DECISIVE ASSERTION: the manually-resumed turn REALLY
        # executed a project tool — Studio manual repair is not a text blob.
        marker = project / _RECOVER_WRITE_PATH
        assert marker.is_file(), \
            'manual resume never executed write_file — Studio manual ' \
            'self-repair degraded to plain text'
        assert marker.read_text() == _RECOVER_WRITE_CONTENT

        # ── BYTE-IDENTITY: done.committedMessage == post-resume DB tail ──
        committed = done.get('committedMessage')
        assert committed, 'done carries no committedMessage'
        after = json.loads(result['conv_messages'])
        tail = after[-1]
        assert tail.get('role') == 'assistant'
        _assert_tail_matches_committed(tail, committed)
        assert tail.get('finishReason') == 'stop'
        assert _RECOVERED_TEXT in (tail.get('content') or '')

        # ── The corpse stayed marked interrupted; the resume row is
        # done; and NO third task row exists — anything the default boot
        # had auto-dispatched would have left one ──
        assert rows.get(task_id) == 'interrupted', rows
        assert rows.get(resume_tid) == 'done', rows
        extra_rows = set(rows) - {task_id, resume_tid}
        assert not extra_rows, \
            f'unexpected extra task row(s) — something auto-dispatched: {extra_rows}'

        # ── Independent parent-side re-read ──
        messages_p, _settings_p = _conv_tail(conv_id)
        _assert_tail_matches_committed(messages_p[-1], committed)
    finally:
        mock.close()


@pytest.mark.unit
def test_NEUTER_default_mode_resume_neutered_write(flask_client, tmp_path):
    """NEUTER proof for Layer 2b (owner gate #3).

    Instance B's write_file EXECUTION handler is amputated BEFORE the
    manual resume (env switch in the boot script — monkeypatch cannot
    cross the process boundary). The resume still runs to completion (the
    tool result is the neuter string), but NO file may land on disk —
    Layer 2b's decisive disk assertion goes red under this mutation.
    """
    project = tmp_path / 'proj'
    project.mkdir()
    mock = _MockLLM(scenario='studio_recover_write')
    conv_id = 'test-conv-manual-nc-' + uuid.uuid4().hex[:12]
    try:
        _send_and_kill_studio_turn(
            flask_client, conv_id, project, str(tmp_path / 'cfg'),
            mock.base_url)

        result = _launch_instance_b(tmp_path, conv_id,
                                    mock_url=mock.base_url,
                                    mode='manual', neuter_write=True,
                                    project_path=str(project))

        assert result.get('write_neutered') is True
        # The resume REALLY ran (the mutation does not block the turn):
        assert result.get('resume_task_id'), result.get('continue_data')
        frames = _parse_sse_frames(result.get('sse_raw') or '')
        done = [e for e in frames if isinstance(e, dict) and e.get('type') == 'done']
        assert done and done[-1].get('finishReason') == 'stop', \
            f'neutered resume did not complete: {[e.get("type") for e in frames]}'
        # Sanity: the model DID request the write and the round closed.
        streams = [h for h in mock.history if h.get('stream')]
        assert any(m.get('role') == 'tool'
                   and m.get('tool_call_id') == 'call_e2e_recover'
                   for h in streams[1:] for m in (h.get('messages') or [])), \
            'neutered run: the write_file round did not even happen'

        # THE NEUTERED OUTCOME: Layer 2b's decisive assertion is red here.
        assert not (project / _RECOVER_WRITE_PATH).exists(), \
            'NEUTER failed: marker written despite the amputated handler'
    finally:
        mock.close()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-v']))
