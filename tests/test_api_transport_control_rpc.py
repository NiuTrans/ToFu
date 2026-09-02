"""Typed transport chooses control RPC only for constrained-proxy reads."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit


def _run(source: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node is required')
    bundle = native_module_path(
        'api-transport-control-rpc.js', 'frontend/src/api/transport.ts')
    completed = subprocess.run(
        ['node', '-e', source.replace('BUNDLE_PATH', json.dumps(bundle))],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_constrained_proxy_prefers_rpc_and_falls_back_only_when_unavailable():
    result = _run(r"""
      (async () => {
        global.window = globalThis;
        global.location = {pathname:'/proxy/15000/'};
        global.document = {getElementById:() => ({
          textContent:JSON.stringify({transportProfile:'constrained-proxy'}),
        })};
        global.sessionStorage = {getItem:() => null, setItem:() => {}};
        let fetches = 0;
        global.fetch = async () => {
          fetches += 1;
          return new Response(JSON.stringify({ok:true,path:'/http'}), {
            status:200, headers:{'content-type':'application/json'},
          });
        };
        let rpcCalls = 0;
        global.pushRpcRequest = async (_method, params) => {
          rpcCalls += 1;
          return {ok:true,path:params.path,via:'rpc'};
        };
        require(BUNDLE_PATH);
        const rpcResult = await request('/api/v1/project/browse', {
          method:'POST', json:{path:'/rpc'}, govern:true,
          rpcMethod:'project.browse', rpcParams:{path:'/rpc'}, timeout:1000,
        });

        global.pushRpcRequest = async () => {
          const error = new Error('socket unavailable');
          error.code = 'rpc_unavailable';
          throw error;
        };
        const fallbackResult = await request('/api/v1/project/browse', {
          method:'POST', json:{path:'/http'}, govern:true,
          rpcMethod:'project.browse', rpcParams:{path:'/http'}, timeout:1000,
        });

        global.pushRpcRequest = async () => {
          const error = new Error('at capacity');
          error.code = -32001;
          throw error;
        };
        let overloadStatus = 0;
        try {
          await request('/api/v1/project/browse', {
            method:'POST', json:{path:'/no-fallback'}, govern:true,
            rpcMethod:'project.browse', rpcParams:{path:'/no-fallback'},
            timeout:1000,
          });
        } catch (error) { overloadStatus = error.status; }

        console.log(JSON.stringify({
          rpcPath:rpcResult.path,
          via:rpcResult.via,
          fallbackPath:fallbackResult.path,
          rpcCalls,
          fetches,
          overloadStatus,
        }));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """)
    assert result == {
        'rpcPath': '/rpc',
        'via': 'rpc',
        'fallbackPath': '/http',
        'rpcCalls': 1,
        'fetches': 1,
        'overloadStatus': 503,
    }


def test_direct_profile_keeps_http_as_the_only_transport():
    result = _run(r"""
      (async () => {
        global.window = globalThis;
        global.location = {pathname:'/proxy/15000/'};
        global.document = {getElementById:() => ({
          textContent:JSON.stringify({transportProfile:'direct'}),
        })};
        global.sessionStorage = {getItem:() => null, setItem:() => {}};
        let rpcCalls = 0;
        let fetches = 0;
        global.pushRpcRequest = async () => { rpcCalls += 1; return {}; };
        global.fetch = async () => {
          fetches += 1;
          return new Response(JSON.stringify({ok:true,path:'/direct'}), {
            status:200, headers:{'content-type':'application/json'},
          });
        };
        require(BUNDLE_PATH);
        const value = await request('/api/v1/project/browse', {
          method:'POST', json:{path:'/direct'},
          rpcMethod:'project.browse', rpcParams:{path:'/direct'},
        });
        console.log(JSON.stringify({path:value.path,rpcCalls,fetches}));
      })().catch((error) => { console.error(error); process.exitCode = 1; });
    """)
    assert result == {'path': '/direct', 'rpcCalls': 0, 'fetches': 1}
