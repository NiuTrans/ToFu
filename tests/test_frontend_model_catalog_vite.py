"""Behavior contracts for the native model-catalog editor.

The tests bundle and execute the real TypeScript owners.  They protect the
cached-envelope isolation and the asynchronous CAS UI states without relying
on implementation-text anchors.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._jsdom import node_deps_available


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / "scripts/vite_test_bundle.mjs"
MODEL = ROOT / "frontend/src/features/model-catalog/model.ts"
PANEL = ROOT / "frontend/src/features/model-catalog/panel.ts"


def _node() -> str:
    executable = shutil.which("node")
    if not executable:
        pytest.skip("node not available")
    return executable


def _bundle(entry: Path, output: Path) -> None:
    compiled = subprocess.run(
        [
            str(BUNDLER),
            str(entry),
            "--bundle",
            "--format=cjs",
            "--platform=node",
            f"--outfile={output}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr


@pytest.mark.skipif(not BUNDLER.is_file(), reason="Vite test bundler unavailable")
def test_catalog_clone_isolates_every_nested_wire_value(tmp_path: Path) -> None:
    built = tmp_path / "model-catalog-model.cjs"
    _bundle(MODEL, built)
    harness = textwrap.dedent(
        """
        const assert = require('node:assert/strict');
        const model = require(BUILT_PATH);
        const original = {
          contract_version: 'tofu.model-catalog/v1',
          revision: 4,
          models: {
            demo: {
              model_id: 'demo', enabled: true, capabilities: ['text'],
              provenance: { source: { kind: 'manual' } },
            },
          },
          offerings: {
            'alpha:demo': {
              offering_id: 'alpha:demo', provider_id: 'alpha', model_id: 'demo',
              enabled: true,
              configuration: {
                request_ids: ['wire-demo'], capabilities: ['text'],
                pricing: { input: 1, output: 2, currency: 'USD' },
              },
              provenance: { source: { account: 'primary' } },
            },
          },
          routes: {
            demo: {
              model_id: 'demo', offering_ids: ['alpha:demo'], strategy: 'score',
              policy: { fallback_order: ['alpha:demo'] },
            },
          },
        };
        const before = JSON.stringify(original);
        const draft = model.cloneCatalog(original);
        draft.models.demo.capabilities.push('vision');
        draft.models.demo.provenance.source.kind = 'generated';
        draft.offerings['alpha:demo'].configuration.request_ids.push('wire-alt');
        draft.offerings['alpha:demo'].configuration.pricing.input = 99;
        draft.offerings['alpha:demo'].provenance.source.account = 'secondary';
        draft.routes.demo.offering_ids.push('beta:demo');
        draft.routes.demo.policy.fallback_order.reverse();
        model.attachOffering(draft, {
          modelId: 'demo', providerId: 'beta',
          configuration: { request_ids: ['beta-demo'], capabilities: ['text'] },
        });

        assert.equal(JSON.stringify(original), before);
        assert.deepEqual(original.models.demo.capabilities, ['text']);
        assert.deepEqual(original.offerings['alpha:demo'].configuration.request_ids, ['wire-demo']);
        assert.equal(original.offerings['alpha:demo'].configuration.pricing.input, 1);
        assert.deepEqual(original.routes.demo.offering_ids, ['alpha:demo']);
        assert.notStrictEqual(draft.models.demo, original.models.demo);
        assert.notStrictEqual(
          draft.offerings['alpha:demo'].configuration,
          original.offerings['alpha:demo'].configuration,
        );
        assert.equal(model.offeringHealthy(original.offerings['alpha:demo']), false);
        assert.equal(model.offeringHealthy(original.offerings['alpha:demo'], {}), false);
        assert.equal(model.offeringHealthy(original.offerings['alpha:demo'], {
          'alpha:demo': { healthy: false, status: 'unknown' },
        }), false);
        assert.equal(model.offeringHealthy(original.offerings['alpha:demo'], {
          'alpha:demo': { healthy: true, status: 'healthy' },
        }), true);
        console.log(JSON.stringify({ isolated: true, unknownHealthFailsClosed: true }));
        """
    ).replace("BUILT_PATH", json.dumps(str(built)))
    run = subprocess.run(
        [_node(), "-e", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (run.stdout or "") + (run.stderr or "")
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {
        "isolated": True,
        "unknownHealthFailsClosed": True,
    }


@pytest.mark.skipif(
    not BUNDLER.is_file() or not node_deps_available(),
    reason="jsdom + Vite test bundler unavailable",
)
def test_catalog_cas_controls_reenable_and_failed_conflict_refresh_stays_error(
    tmp_path: Path,
) -> None:
    built = tmp_path / "model-catalog-panel.cjs"
    _bundle(PANEL, built)
    harness = textwrap.dedent(
        """
        const assert = require('node:assert/strict');
        const { JSDOM } = require('jsdom');
        const dom = new JSDOM(
          '<!doctype html><body><div id="stgModelCatalogList"></div></body>',
          { url: 'http://localhost:15000/' },
        );
        global.window = dom.window;
        global.document = dom.window.document;
        global.location = dom.window.location;
        global.sessionStorage = dom.window.sessionStorage;
        for (const name of [
          'Element', 'HTMLElement', 'HTMLInputElement', 'HTMLSelectElement',
          'HTMLButtonElement', 'Event', 'AbortController', 'AbortSignal',
        ]) global[name] = dom.window[name];
        window.t = (key) => key;

        function catalog(enabled) {
          return {
            contract_version: 'tofu.model-catalog/v1', revision: enabled ? 1 : 2,
            models: {
              demo: {
                model_id: 'demo', display_name: 'Demo', enabled,
                capabilities: ['text'], provenance: {},
              },
            },
            offerings: {
              'alpha:demo': {
                offering_id: 'alpha:demo', provider_id: 'alpha', model_id: 'demo',
                enabled, configuration: {
                  request_ids: ['demo'], capabilities: ['text'],
                }, provenance: {},
              },
            },
            routes: {
              demo: {
                model_id: 'demo', offering_ids: ['alpha:demo'], strategy: 'score',
              },
            },
          };
        }
        function envelope(value, revision) {
          return {
            ok: true, contract_version: 'tofu.model-catalog/v1', revision,
            catalog: value,
            providers: { alpha: { id: 'alpha', name: 'Alpha', protocol: 'openai' } },
            health: {},
          };
        }
        function response(status, body) {
          return {
            ok: status >= 200 && status < 300,
            status,
            headers: { get: (name) => (
              String(name).toLowerCase() === 'content-type'
                ? 'application/json' : null
            ) },
            async json() { return body; },
            async text() { return JSON.stringify(body); },
          };
        }

        let gets = 0;
        let puts = 0;
        let resolveFirstPut;
        global.fetch = window.fetch = async (_url, init = {}) => {
          const method = String(init.method || 'GET').toUpperCase();
          if (method === 'GET') {
            gets += 1;
            if (gets === 1) return response(200, envelope(catalog(true), 1));
            if (gets === 2) throw new Error('refresh offline');
            if (gets === 3) {
              const missing = envelope(catalog(true), 0);
              delete missing.revision;
              delete missing.catalog.revision;
              return response(200, missing);
            }
            if (gets === 4) {
              const invalid = envelope(catalog(true), 0);
              invalid.revision = 'not-an-integer';
              delete invalid.catalog.revision;
              return response(200, invalid);
            }
            if (gets === 5) {
              const missingVersion = envelope(catalog(true), 0);
              delete missingVersion.contract_version;
              return response(200, missingVersion);
            }
            if (gets === 6) {
              const wrongVersion = envelope(catalog(true), 0);
              wrongVersion.contract_version = 'tofu.model-catalog/v2';
              return response(200, wrongVersion);
            }
            if (gets === 7) {
              const wrongCatalogVersion = envelope(catalog(true), 0);
              wrongCatalogVersion.catalog.contract_version = 'tofu.model-catalog/v2';
              return response(200, wrongCatalogVersion);
            }
          }
          if (method === 'PUT') {
            puts += 1;
            if (puts === 1) {
              const payload = JSON.parse(init.body);
              return new Promise((resolve) => {
                resolveFirstPut = () => resolve(response(
                  200, envelope(payload.catalog, 2),
                ));
              });
            }
            if (puts === 2) {
              return response(409, {
                error: { kind: 'conflict', message: 'revision conflict' },
              });
            }
          }
          throw new Error(`unexpected request ${method} #${method === 'GET' ? gets : puts}`);
        };

        const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
        async function settle() {
          for (let index = 0; index < 8; index += 1) await tick();
        }
        function controls() {
          return {
            add: document.querySelector('[data-mc-add]'),
            refresh: document.querySelector('[data-mc-refresh]'),
            attach: document.querySelector('[data-mc-attach]'),
          };
        }
        const owner = require(BUILT_PATH);

        (async () => {
          await owner.renderModelCatalogPanel();
          assert.equal(
            document.querySelector('.stg-mc-offering-health').classList.contains('unhealthy'),
            true,
          );
          let toggle = document.querySelector('[data-mc-toggle="logical"]');
          toggle.checked = false;
          toggle.dispatchEvent(new window.Event('change', { bubbles: true }));
          await tick();
          const pending = controls();
          assert.equal(pending.add.disabled, true);
          assert.equal(pending.refresh.disabled, true);
          assert.equal(pending.attach.disabled, true);

          resolveFirstPut();
          await settle();
          const afterSuccess = controls();
          assert.equal(afterSuccess.add.disabled, false);
          assert.equal(afterSuccess.refresh.disabled, false);
          assert.equal(afterSuccess.attach.disabled, false);

          toggle = document.querySelector('[data-mc-toggle="logical"]');
          toggle.checked = true;
          toggle.dispatchEvent(new window.Event('change', { bubbles: true }));
          await settle();
          const status = document.querySelector('.stg-mc-status').textContent;
          const afterConflictFailure = controls();
          assert.match(status, /refresh offline/);
          assert.doesNotMatch(status, /latest revision/i);
          assert.equal(afterConflictFailure.add.disabled, false);
          assert.equal(afterConflictFailure.refresh.disabled, false);
          assert.equal(afterConflictFailure.attach.disabled, false);
          assert.equal(gets, 2);
          assert.equal(puts, 2);

          const rejectedRevisionStatuses = [];
          for (let index = 0; index < 2; index += 1) {
            owner.destroyModelCatalogPanel();
            await owner.renderModelCatalogPanel();
            toggle = document.querySelector('[data-mc-toggle="logical"]');
            toggle.checked = false;
            toggle.dispatchEvent(new window.Event('change', { bubbles: true }));
            await settle();
            rejectedRevisionStatuses.push(
              document.querySelector('.stg-mc-status').textContent,
            );
            assert.equal(puts, 2);
          }
          assert.equal(gets, 4);
          assert.deepEqual(rejectedRevisionStatuses, [
            'Failed to load the model catalog.',
            'Failed to load the model catalog.',
          ]);

          const rejectedContractStatuses = [];
          for (let index = 0; index < 3; index += 1) {
            owner.destroyModelCatalogPanel();
            await owner.renderModelCatalogPanel();
            assert.equal(document.querySelector('[data-mc-toggle="logical"]'), null);
            assert.equal(puts, 2);
            const state = document.querySelector('.stg-mc-state-text').textContent;
            assert.match(state, /Failed to load the model catalog[.]/);
            rejectedContractStatuses.push(state);
          }
          assert.equal(gets, 7);
          owner.destroyModelCatalogPanel();
          console.log(JSON.stringify({
            status, gets, puts, controlsEnabled: true,
            rejectedRevisionStatuses,
            rejectedContractStatuses,
            unknownHealthFailsClosed: true,
            contractVersionFailsClosed: true,
          }));
        })().catch((error) => {
          console.error(error && error.stack || error);
          process.exitCode = 1;
        });
        """
    ).replace("BUILT_PATH", json.dumps(str(built)))
    run = subprocess.run(
        [_node(), "-e", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (run.stdout or "") + (run.stderr or "")
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result == {
        "status": "refresh offline",
        "gets": 7,
        "puts": 2,
        "controlsEnabled": True,
        "rejectedRevisionStatuses": [
            "Failed to load the model catalog.",
            "Failed to load the model catalog.",
        ],
        "rejectedContractStatuses": [
            "Failed to load the model catalog. Failed to load the model catalog.",
            "Failed to load the model catalog. Failed to load the model catalog.",
            "Failed to load the model catalog. Failed to load the model catalog.",
        ],
        "unknownHealthFailsClosed": True,
        "contractVersionFailsClosed": True,
    }
