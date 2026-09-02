"""Browser contract for the VS Code proxy request governor."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit


def test_server_publishes_only_an_overridable_transport_profile(monkeypatch):
    from routes.common import _browser_transport_profile

    monkeypatch.setenv('VSCODE_PROXY_URI', 'https://gateway.invalid/{{port}}/')
    monkeypatch.delenv('TOFU_PROXY_TRANSPORT_PROFILE', raising=False)
    assert _browser_transport_profile() == 'constrained-proxy'

    monkeypatch.setenv('TOFU_PROXY_TRANSPORT_PROFILE', 'direct')
    assert _browser_transport_profile() == 'direct'

    monkeypatch.delenv('VSCODE_PROXY_URI', raising=False)
    monkeypatch.setenv(
        'TOFU_PROXY_TRANSPORT_PROFILE', 'constrained-proxy')
    assert _browser_transport_profile() == 'constrained-proxy'


def _run_node(source: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node is required')
    bundle = native_module_path(
        'api-transport-proxy-governor.js',
        'frontend/src/api/transport.ts',
    )
    completed = subprocess.run(
        ['node', '-e', source.replace('BUNDLE_PATH', json.dumps(bundle))],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_constrained_proxy_bounds_prioritizes_coalesces_and_cancels_reads():
    result = _run_node(r"""
      (async () => {
        global.window = globalThis;
        global.location = { pathname: '/proxy/15000/' };
        global.document = { getElementById: () => ({
          textContent: JSON.stringify({transportProfile:'constrained-proxy'}),
        }) };
        global.sessionStorage = { getItem: () => null, setItem: () => {} };
        require(BUNDLE_PATH);
        const tick = () => new Promise((resolve) => setImmediate(resolve));
        const jsonResponse = (url) => new Response(JSON.stringify({url}), {
          status: 200, headers: {'content-type':'application/json'},
        });

        let active = 0;
        let maxActive = 0;
        global.fetch = async (url) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          await new Promise((resolve) => setTimeout(resolve, 8));
          active -= 1;
          return jsonResponse(String(url));
        };
        await Promise.all(Array.from({length: 18}, (_, index) =>
          request('/burst/' + index, {priority:'background'})));

        let fetchCount = 0;
        global.fetch = async (url) => {
          fetchCount += 1;
          await new Promise((resolve) => setTimeout(resolve, 5));
          return jsonResponse(String(url));
        };
        const [sameA, sameB] = await Promise.all([
          request('/same', {query:{page:1}, coalesce:true}),
          request('/same', {query:{page:1}, coalesce:true}),
        ]);

        const startOrder = [];
        const blockers = [];
        global.fetch = (url) => {
          const value = String(url);
          startOrder.push(value);
          if (value.includes('/block/')) {
            return new Promise((resolve) => blockers.push(
              () => resolve(jsonResponse(value))));
          }
          return Promise.resolve(jsonResponse(value));
        };
        const held = Array.from({length: 6}, (_, index) =>
          request('/block/' + index));
        await tick();
        const queuedBackground = request('/queued-background', {
          priority:'background',
        });
        const queuedForeground = request('/queued-foreground', {
          priority:'foreground',
        });
        await tick();
        blockers.shift()();
        await tick();
        const firstQueuedStart = startOrder[6] || '';
        while (blockers.length) blockers.shift()();
        await Promise.all([...held, queuedBackground, queuedForeground]);

        const abortStarts = [];
        const abortBlockers = [];
        global.fetch = (url) => {
          const value = String(url);
          abortStarts.push(value);
          return new Promise((resolve) => abortBlockers.push(
            () => resolve(jsonResponse(value))));
        };
        const abortHeld = Array.from({length: 6}, (_, index) =>
          request('/abort-block/' + index));
        await tick();
        const controller = new AbortController();
        const cancelled = request('/must-not-start', {signal:controller.signal});
        controller.abort(new Error('cancelled while queued'));
        let cancelRejected = false;
        try { await cancelled; } catch { cancelRejected = true; }
        while (abortBlockers.length) abortBlockers.shift()();
        await Promise.all(abortHeld);

        console.log(JSON.stringify({
          maxActive,
          fetchCount,
          coalescedEqual: sameA.url === sameB.url,
          foregroundFirst: firstQueuedStart.includes('/queued-foreground'),
          cancelRejected,
          cancelledNeverStarted: !abortStarts.some(
            (url) => url.includes('/must-not-start')),
        }));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """)
    assert result == {
        'maxActive': 6,
        'fetchCount': 1,
        'coalescedEqual': True,
        'foregroundFirst': True,
        'cancelRejected': True,
        'cancelledNeverStarted': True,
    }


def test_direct_profile_does_not_throttle_read_concurrency():
    result = _run_node(r"""
      (async () => {
        global.window = globalThis;
        // Explicit direct is the rollback seam and must override path-shape
        // fallback even when the URL still contains /proxy/<port>/.
        global.location = { pathname: '/proxy/15000/' };
        global.document = { getElementById: () => ({
          textContent: JSON.stringify({transportProfile:'direct'}),
        }) };
        global.sessionStorage = { getItem: () => null, setItem: () => {} };
        let active = 0;
        let maxActive = 0;
        global.fetch = async () => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          await new Promise((resolve) => setTimeout(resolve, 8));
          active -= 1;
          return new Response('{}', {
            status:200, headers:{'content-type':'application/json'},
          });
        };
        require(BUNDLE_PATH);
        await Promise.all(Array.from({length:10}, (_, index) =>
          request('/direct/' + index)));
        console.log(JSON.stringify({maxActive}));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """)
    assert result == {'maxActive': 10}
