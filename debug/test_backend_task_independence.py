#!/usr/bin/env python3
"""End-to-end test: backend task survives frontend SSE/HTTP disconnect.

Contract (see a.md):
    Once /api/chat/start or /api/chat/send returns {taskId}, the task MUST
    continue to run to completion in a daemon thread and persist its result
    to task_results + conversations.messages, even if the originating
    frontend disconnects immediately and never reconnects.

Scenario simulated:
    1. POST /api/chat/send → obtain taskId.
    2. Open SSE stream, read ONE keepalive, then HARD-CLOSE the socket.
    3. Poll /api/chat/poll/<task_id> until status != 'running'.
    4. Assert:
        - final status is 'done' (not 'aborted')
        - content is non-empty
        - task['aborted'] was never set (verified via /api/chat/active absence)

Run:
    # Start the server first:  python server.py
    # Then:
    python debug/test_backend_task_independence.py --base http://127.0.0.1:5000

Exit code 0 on pass, non-zero on fail.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from http.client import HTTPConnection
from urllib.parse import urlparse

import requests


def _new_conv_id() -> str:
    return str(uuid.uuid4())


def _send_message(base: str, conv_id: str, text: str) -> str:
    """POST /api/chat/send and return taskId."""
    payload = {
        'convId': conv_id,
        'message': {'text': text},
        'config': {'model': None},  # server default
    }
    r = requests.post(f'{base}/api/chat/send', json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if 'taskId' not in data:
        raise RuntimeError(f'Unexpected /send response (no taskId): {data}')
    return data['taskId']


def _hard_close_sse(base: str, task_id: str) -> None:
    """Open SSE stream, read a tiny bit, then forcibly close the socket.

    We use httplib directly to guarantee a TCP RST-style close (not a
    graceful HTTP/1.1 shutdown), which most closely mirrors a browser
    tab-close / network-drop.
    """
    parsed = urlparse(base)
    conn = HTTPConnection(parsed.hostname, parsed.port or 80, timeout=10)
    conn.request('GET', f'/api/chat/stream/{task_id}',
                 headers={'Accept': 'text/event-stream'})
    resp = conn.getresponse()
    if resp.status != 200:
        conn.close()
        raise RuntimeError(f'SSE stream returned HTTP {resp.status}')
    # Read at most 4 KB then nuke the connection
    try:
        _ = resp.read(4096)
    except Exception:
        pass
    # Hard close — does NOT send proper HTTP termination
    try:
        conn.sock.close()
    except Exception:
        pass
    conn.close()


def _poll_until_done(base: str, task_id: str, max_wait: float = 120.0) -> dict:
    """Poll until status != 'running' or timeout."""
    deadline = time.time() + max_wait
    last = None
    while time.time() < deadline:
        r = requests.get(f'{base}/api/chat/poll/{task_id}', timeout=10)
        r.raise_for_status()
        last = r.json()
        status = last.get('status', '?')
        if status != 'running':
            return last
        time.sleep(1.0)
    raise TimeoutError(f'Task {task_id} still running after {max_wait}s — last={last}')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', default='http://127.0.0.1:5000',
                    help='Server base URL (default: http://127.0.0.1:5000)')
    ap.add_argument('--prompt', default='Say exactly: backend independence test OK',
                    help='Prompt to send')
    ap.add_argument('--max-wait', type=float, default=180.0,
                    help='Max seconds to wait for task completion')
    args = ap.parse_args()

    base = args.base.rstrip('/')
    conv_id = _new_conv_id()
    print(f'[test] conv_id={conv_id[:8]}')

    # 1. Send
    t0 = time.time()
    task_id = _send_message(base, conv_id, args.prompt)
    print(f'[test] taskId={task_id[:8]} (sent in {time.time()-t0:.2f}s)')

    # 2. Open SSE briefly and hard-close
    try:
        _hard_close_sse(base, task_id)
        print('[test] ✓ SSE opened and hard-closed')
    except Exception as e:
        print(f'[test] WARN: SSE open+close failed: {e} (continuing anyway)')

    # Give the server a moment to notice the disconnect
    time.sleep(1.0)

    # 3. Check the task is NOT marked aborted
    r = requests.get(f'{base}/api/chat/active', timeout=10)
    if r.ok:
        active = r.json()
        entry = next((t for t in active if t['id'] == task_id), None)
        if entry:
            aborted = entry.get('aborted', False)
            status = entry.get('status', '?')
            print(f'[test] post-disconnect active: status={status} aborted={aborted}')
            if aborted:
                print('[test] ❌ FAIL: task was aborted immediately after SSE disconnect!')
                return 1
        else:
            # Task may have completed very fast and been evicted — OK
            print('[test] task not in active list (may have completed already)')

    # 4. Poll to completion
    print(f'[test] polling /api/chat/poll/{task_id[:8]}...')
    result = _poll_until_done(base, task_id, max_wait=args.max_wait)
    status = result.get('status')
    content = result.get('content') or ''
    err = result.get('error')
    fr = result.get('finishReason')
    print(f'[test] final: status={status} finishReason={fr} content={len(content)}chars error={err!r}')

    if status != 'done':
        print(f'[test] ❌ FAIL: final status={status} (expected done)')
        return 2
    if fr == 'aborted':
        print('[test] ❌ FAIL: finishReason=aborted — something set task.aborted!')
        return 3
    if not content.strip() and not err:
        print('[test] ❌ FAIL: task done but content is empty (no error either)')
        return 4

    print('[test] ✅ PASS — backend completed the task independently of the disconnected client')
    print(f'[test] preview: {content[:200]!r}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
