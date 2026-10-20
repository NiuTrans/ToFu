"""Settings Save refreshes the config projection without refetching routing.

Settings Save commits the routing authority through its own PUT and already
holds the committed document. A full startup pass after that save re-downloaded
the ~550KB aggregate plus its parse cost on the main thread, inside the window
where the user watches the save. `refreshConfig()` is the config-only seam; the
failure path must still route through the full startup ladder that owns retries.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..'))
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')
ENTRY = os.path.join(ROOT, 'frontend/src/features/model-catalog/model-picker-startup.ts')

HARNESS = r"""
const { createModelPickerStartup } = require(BUILT);

function makePorts(mode) {
  const calls = { config: 0, routing: 0, applied: [], failures: [] };
  const ports = {
    loadConfig: () => {
      calls.config += 1;
      return mode === 'config-fails'
        ? Promise.reject(Object.assign(new Error('boom'), { status: 503 }))
        : Promise.resolve({ presets: {} });
    },
    loadRouting: () => {
      calls.routing += 1;
      return mode === 'config-fails'
        ? Promise.reject(Object.assign(new Error('boom'), { status: 503 }))
        : Promise.resolve({ model_routing: { contract_version: 'tofu.model-routing/v2' } });
    },
    applyConfig: config => calls.applied.push(config),
    applyRouting: () => {},
    showFailure: error => calls.failures.push(String(error && error.message)),
    onBootReady: () => () => {},
    isActive: () => true,
    scheduleRetry: () => null,
    cancelRetry: () => {},
  };
  return { calls, ports };
}

async function run(mode) {
  const { calls, ports } = makePorts(mode);
  const owner = createModelPickerStartup(ports);
  let rejected = false;
  await owner.refreshConfig().catch(() => { rejected = true; });
  // The failure path falls back to the full startup pass, which is async.
  await new Promise(resolve => setTimeout(resolve, 0));
  owner.destroy();
  return {
    configReads: calls.config,
    routingReads: calls.routing,
    applied: calls.applied.length,
    failures: calls.failures.length,
    rejected,
  };
}

(async () => {
  console.log(JSON.stringify({
    saved: await run('ok'),
    failing: await run('config-fails'),
  }));
})();
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_refresh_config_reads_config_only_and_falls_back_on_failure(tmp_path):
    built = tmp_path / 'model-picker-startup.js'
    compiled = subprocess.run(
        [ESBUILD, ENTRY, '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert compiled.returncode == 0, compiled.stderr
    script = HARNESS.replace('BUILT', json.dumps(str(built)))
    result = subprocess.run(
        ['node', '-e', script], cwd=ROOT, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])

    saved = observed['saved']
    assert saved['configReads'] == 1
    assert saved['routingReads'] == 0, 'a successful save must not refetch routing'
    assert saved['applied'] == 1
    assert saved['failures'] == 0

    failing = observed['failing']
    assert failing['rejected'] is True
    assert failing['routingReads'] >= 1, 'recovery must use the full startup ladder'
