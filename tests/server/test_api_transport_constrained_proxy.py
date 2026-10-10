"""Browser contract for the constrained-proxy transport profile.

The profile still gates the WebSocket control-RPC lane and explicit
single-flight coalescing, but there is NO client-side read queue: every
request executes immediately. The old governor (6 concurrent reads +
priority queue) let a slow post-restart boot salvo starve foreground user
actions for tens of seconds behind cold-storage reads — queueing cost far
more than proxy admission ever saved, so it was removed.
"""

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
        'api-transport-constrained-proxy.js',
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


def test_constrained_proxy_never_queues_reads_but_still_coalesces():
    result = _run_node(r"""
      (async () => {
        global.window = globalThis;
        global.location = { pathname: '/proxy/15000/' };
        global.document = { getElementById: () => ({
          textContent: JSON.stringify({transportProfile:'constrained-proxy'}),
        }) };
        global.sessionStorage = { getItem: () => null, setItem: () => {} };
        require(BUNDLE_PATH);
        const jsonResponse = (url) => new Response(JSON.stringify({url}), {
          status: 200, headers: {'content-type':'application/json'},
        });

        // No read queue: every request starts immediately, far past the old
        // 6-read budget, so a slow boot salvo can no longer starve a
        // foreground user action.
        let active = 0;
        let maxActive = 0;
        global.fetch = async (url) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          await new Promise((resolve) => setTimeout(resolve, 8));
          active -= 1;
          return jsonResponse(String(url));
        };
        await Promise.all([
          ...Array.from({length: 10}, (_, index) =>
            request('/burst/' + index, {priority:'background'})),
          request('/fg-click', {priority:'foreground'}),
        ]);

        // Explicit single-flight coalescing of identical safe GETs stays.
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

        console.log(JSON.stringify({
          maxActive,
          fetchCount,
          coalescedEqual: sameA.url === sameB.url,
        }));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """)
    assert result == {
        'maxActive': 11,
        'fetchCount': 1,
        'coalescedEqual': True,
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
