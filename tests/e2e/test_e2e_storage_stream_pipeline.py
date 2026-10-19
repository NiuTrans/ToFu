"""Native durability -> actual push transport -> real browser text anchors.

Only upstream tokens are synthetic. Storage, task FIFO, HTTP, push and DOM are
production paths; an explicit held durability gate proves no premature paint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest

from tests.e2e.test_e2e_smoke import (
    _disable_open_mode_rate_limit, _install_llm_stubs,
    _select_stub_model, _wait_app_ready,
)

pytestmark = pytest.mark.visual


def test_native_stream_durability_push_and_browser_anchors(page, monkeypatch):
    from lib.storage import get_storage_client
    from lib.storage.v2_client import StorageV2Client
    if not isinstance(get_storage_client(), StorageV2Client):
        pytest.skip('requires native TofuDB: TOFU_PERSONAL_STORAGE_BACKEND=tofudb')
    from lib import turn_lifecycle
    from lib.agent_core import push
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager._events import append_event
    from lib.tasks_pkg.event_log import read_events
    import lib.tasks_pkg.llm_fallback._call as llm_call

    markers = [f"pipeline_anchor_{index:03d}" for index in range(8)]
    held, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    anchors, failures = {}, []
    original_record, original_push = turn_lifecycle.record_task_event, push.push_event
    from tofu_db.wire import StorageV2Session
    original_request = StorageV2Session.request
    def acknowledged_request(session, operation, payload, **kwargs):
        response = original_request(session, operation, payload, **kwargs)
        if response.status == 0 and operation in {'turn.event.record', 'turn.event.record_batch', 'event.append_batch'}:
            encoded = json.dumps(payload)
            acknowledged_at = now_ms()
            with lock:
                for marker in markers:
                    if marker in encoded and marker in anchors:
                        anchors[marker].setdefault('storage_ack_ms', acknowledged_at)
        return response

    active_task = {}
    finished = threading.Event()

    def now_ms():
        return time.time_ns() / 1_000_000

    def source(task, body, tag='', on_tool_call_ready=None):
        active_task['id'] = task['id']
        try:
            for marker in markers:
                with lock:
                    anchors[marker] = {'provider_emit_ms': now_ms()}
                with task['content_lock']:
                    task['content'] += marker + ' '
                append_event(task, build_event(EventType.DELTA, content=marker + ' '))
                time.sleep(0.03)
            return ({'role': 'assistant', 'content': ' '.join(markers) + ' ', 'tool_calls': []},
                'stop', {'prompt_tokens': 8, 'completion_tokens': 8, 'total_tokens': 16})
        finally:
            finished.set()

    def record(task, event, **kwargs):
        if event.get('type') == 'delta' and markers[0] in event.get('content', ''):
            held.set()
            if not release.wait(15):
                raise TimeoutError('browser durability test did not release its gate')
        return original_record(task, event, **kwargs)

    def observed_push(channel, task_id, event, *, user_id):
        if task_id == active_task.get('id') and event.get('type') == 'delta':
            content = event.get('content', '')
            selected = [marker for marker in markers if marker in content]
            if selected:
                sequence = event.get('seq')
                rows = read_events(task_id, since_event_id=None)
                if sequence not in {row['event_id'] for row in rows}:
                    failures.append(f'push {sequence} has no durable event')
                with lock:
                    for marker in selected:
                        anchors[marker]['durable_readback_ms'] = now_ms()
                        anchors[marker]['push_ms'] = now_ms()
        return original_push(channel, task_id, event, user_id=user_id)

    monkeypatch.setattr(llm_call, 'stream_llm_response', source)
    monkeypatch.setattr(turn_lifecycle, 'record_task_event', record)
    monkeypatch.setattr(push, 'push_event', observed_push)
    monkeypatch.setattr(StorageV2Session, 'request', acknowledged_request)
    page.context.add_init_script("""(() => {
        const markers = """ + json.dumps([f"pipeline_anchor_{index:03d}" for index in range(8)]) + """;
        window.__storagePipelineReceive = {};
        window.__storagePipelineTransport = {};
        const record = (data, transport) => {
            for (const marker of markers) {
                if (data.includes(marker) && !(marker in window.__storagePipelineReceive)) {
                    window.__storagePipelineReceive[marker] = performance.timeOrigin + performance.now();
                    window.__storagePipelineTransport[marker] = transport;
                }
            }
        };
        const nativeFetch = window.fetch;
        window.fetch = async function(...args) {
            const response = await nativeFetch.apply(this, args);
            const copy = response.clone();
            (async () => {
                if (!copy.body) return;
                const reader = copy.body.getReader(), decoder = new TextDecoder();
                let suffix = '';
                try {
                    for (;;) {
                        const next = await reader.read();
                        if (next.done) break;
                        const text = suffix + decoder.decode(next.value, {stream:true});
                        record(text, 'fetch');
                        suffix = text.slice(-64);
                    }
                } finally { reader.releaseLock(); }
            })().catch(() => {});
            return response;
        };
        const NativeEventSource = window.EventSource;
        window.EventSource = class extends NativeEventSource {
            observedTypes = new Set();
            constructor(...args) {
                super(...args);
                super.addEventListener('message', event => record(event.data, 'sse'));
            }
            addEventListener(type, listener, options) {
                if (!this.observedTypes.has(type)) {
                    this.observedTypes.add(type);
                    super.addEventListener(type, event => {
                        if (typeof event.data === 'string') record(event.data, 'sse');
                    });
                }
                return super.addEventListener(type, listener, options);
            }
        };
        const NativeSocket = window.WebSocket;
        window.WebSocket = class extends NativeSocket {
            constructor(...args) {
                super(...args);
                this.addEventListener('message', event => {
                    if (typeof event.data !== 'string') return;
                    record(event.data, 'websocket');
                });
            }
        };
    })();""")
    page.reload()
    _wait_app_ready(page)
    page.locator('.new-chat-btn').click()
    _select_stub_model(page)
    page.evaluate("""(markers) => {
        window.__storagePipelinePaint = {};
        const observe = () => {
            const text = document.querySelector('#chatInner')?.innerText || '';
            for (const marker of markers) {
                if (text.includes(marker) && !(marker in window.__storagePipelinePaint))
                    window.__storagePipelinePaint[marker] = performance.timeOrigin + performance.now();
            }
        };
        window.__storagePipelineObserver = new MutationObserver(observe);
        window.__storagePipelineObserver.observe(document.querySelector('#chatInner'),
            {subtree: true, childList: true, characterData: true});
    }""", markers)
    try:
        page.locator('#userInput').fill('Measure the durable streaming path')
        page.locator('#sendBtn').click()
        assert held.wait(10), 'synthetic provider did not reach native persistence'
        assert markers[0] not in page.locator('#chatInner').inner_text()
        assert page.evaluate('Object.keys(window.__storagePipelinePaint).length') == 0
        release.set()
        page.wait_for_function("(marker) => document.querySelector('#chatInner')?.innerText.includes(marker)",
            arg=markers[-1], timeout=30000)
        assert finished.wait(5)
        page.wait_for_function("!document.querySelector('#sendBtn')?.classList.contains('stop-btn')", timeout=30000)
        paints = page.evaluate('window.__storagePipelinePaint')
        received = page.evaluate('window.__storagePipelineReceive')
        transports = page.evaluate('window.__storagePipelineTransport')
        assert set(paints) == set(markers)
        assert set(received) == set(markers), 'real transport markers were not observed'
        assert not failures, failures
        with lock:
            rows = [dict(marker=marker, **anchors[marker], dom_ms=paints[marker],
                transport_receive_ms=received.get(marker), transport=transports.get(marker)) for marker in markers]
        assert all('push_ms' in row for row in rows), rows
        for row in rows:
            assert row['provider_emit_ms'] <= row['durable_readback_ms'] <= row['push_ms'], row
            assert row['provider_emit_ms'] <= row['storage_ack_ms'], row
            # Canonical SSE reads committed authority independently of the
            # producer's RPC response. Receipt arrival is measured, but it
            # cannot impose an ordering on that other transport connection.
            assert row['provider_emit_ms'] <= row['transport_receive_ms'] <= row['dom_ms'], row
        # Reconnect renders the exact durable turn, rather than retaining only
        # bytes from the live transport connection.
        page.reload()
        page.wait_for_function("(marker) => document.querySelector('#chatInner')?.innerText.includes(marker)",
            arg=markers[-1], timeout=30000)
        text = page.locator('#chatInner').inner_text()
        assert all(text.count(marker) == 1 for marker in markers)
        destination = os.environ.get('TOFU_STORAGE_BROWSER_ANCHORS_OUTPUT')
        if destination:
            Path(destination).write_text(json.dumps({
                'format': 'tofu.storage-browser-anchors/v1',
                'scope': 'synthetic provider; real task FIFO/native storage/HTTP/push/DOM; held first commit included',
                'clock': 'host wall milliseconds; browser performance epoch',
                'held_gate_no_premature_paint': True, 'reload_exact': True, 'anchors': rows,
            }, indent=2) + '\n')
    finally:
        release.set()
        finished.wait(15)
        page.evaluate('window.__storagePipelineObserver?.disconnect()')
