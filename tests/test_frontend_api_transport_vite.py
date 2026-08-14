"""Vite API transport ownership and deployment-prefix resolution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
ESBUILD = os.path.join(ROOT, 'node_modules/.bin/esbuild')
ENTRY = os.path.join(ROOT, 'frontend/src/api/transport.ts')


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + esbuild unavailable',
)
def test_transport_resolves_main_admin_and_proxy_paths(tmp_path):
    built = tmp_path / 'api-transport.js'
    compiled = subprocess.run(
        [ESBUILD, ENTRY, '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
global.window = global;
global.location = { pathname: '/' };
global.document = {
  config: { entry: 'main' },
  getElementById: () => ({ textContent: JSON.stringify(global.document.config) }),
};
global.sessionStorage = { getItem: () => null, setItem: () => {} };
const TofuNativeApi = require(BUILT);
function at(pathname, entry, target = '/api/v1/auth/mode') {
  global.location.pathname = pathname;
  global.document.config = { entry };
  return TofuNativeApi.resolvePath(target);
}
console.log(JSON.stringify({
  root: at('/', 'main'),
  mainProxy: at('/proxy/15000/', 'main'),
  indexProxy: at('/proxy/15000/index.html', 'main'),
  admin: at('/admin', 'admin'),
  adminSlash: at('/admin/', 'admin'),
  adminProxy: at('/proxy/15000/admin', 'admin'),
  absolute: at('/admin', 'admin', 'https://example.test/value'),
}));
""".replace('BUILT', json.dumps(str(built)))
    proc = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        'root': '/api/v1/auth/mode',
        'mainProxy': '/proxy/15000/api/v1/auth/mode',
        'indexProxy': '/proxy/15000/api/v1/auth/mode',
        'admin': '/api/v1/auth/mode',
        'adminSlash': '/api/v1/auth/mode',
        'adminProxy': '/proxy/15000/api/v1/auth/mode',
        'absolute': 'https://example.test/value',
    }


def test_main_and_admin_publish_one_shared_transport():
    transport = open(ENTRY, encoding='utf-8').read()
    main = open(os.path.join(ROOT, 'frontend/src/main.ts'), encoding='utf-8').read()
    admin = open(os.path.join(ROOT, 'frontend/src/admin.ts'), encoding='utf-8').read()
    runtime = open(os.path.join(
        ROOT, 'frontend/src/runtime/app-runtime.js'), encoding='utf-8').read()
    assert transport.count('fetch(url, init)') == 1
    assert "from './api/transport'" in main
    assert "from './api/transport'" in admin
    assert 'installLegacyApiBindings();' in main
    assert 'global.Api = Api' in runtime
    assert 'publicWindow.Api = apiTransport' in admin
