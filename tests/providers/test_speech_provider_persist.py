"""Behavior contract for the Settings save-time STT provider persist.

The Settings lazy bundle (settings-presenters) owns ``_stgModelRouting`` and
``_stgModelRoutingRevision`` as chunk-local state exposed through live
accessors on ``featureRegistry`` — its composed ``runtimeScope``.  The
app-runtime scope never carries those names, so ``persistSttProvider`` must
read and write them through the registry.  Reading the app-runtime port
instead made every Settings 保存 fail with ``model-routing v2 authority is
not ready`` even though the authority had loaded and rendered.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _node_available() -> bool:
    return bool(shutil.which("node") and (ROOT / "node_modules/jsdom").is_dir())


_HARNESS = textwrap.dedent(
    r"""
    (async () => {
    const fs = require('fs');
    const path = require('path');
    const { JSDOM } = require(path.join(process.argv[1], 'node_modules', 'jsdom'));
    const dom = new JSDOM(`<!doctype html><body>
      <input type="checkbox" id="settingSttEnabled">
      <select id="settingSttProvider"><option value="openai">OpenAI</option></select>
      <div id="sttProviderFields"></div>
      <input id="settingSttModelOpenai">
      <input id="settingSttBaseOpenai">
      <input id="settingSttKeyOpenai">
    </body>`, { url: 'http://localhost/' });

    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.HTMLElement = dom.window.HTMLElement;
    globalThis.HTMLInputElement = dom.window.HTMLInputElement;
    globalThis.HTMLSelectElement = dom.window.HTMLSelectElement;
    window.t = (key) => key;

    (0, eval)(fs.readFileSync(process.argv[2], 'utf8'));

    const namespaceKey = Object.keys(globalThis).find(
      (key) => key.startsWith('TofuNativeTest_'));
    const registry = globalThis[namespaceKey].featureRegistry;

    // Mirror the production settings lazy bundle: chunk-local state exposed
    // only through live accessors on featureRegistry.  The app-runtime scope
    // (window here) never holds these names — that split is the regression
    // surface this spec pins.
    let stagedDocument = null;
    let stagedRevision = 0;
    Object.defineProperties(registry, {
      _stgModelRouting: {
        configurable: true,
        get: () => stagedDocument,
        set: (value) => { stagedDocument = value; },
      },
      _stgModelRoutingRevision: {
        configurable: true,
        get: () => stagedRevision,
        set: (value) => { stagedRevision = Number(value || 0); },
      },
    });

    const emptyDocument = () => ({
      contract_version: 'tofu.model-routing/v2',
      creators: [], models: [], providers: [], provider_accesses: [],
      connections: [], credentials: [], offerings: [], deployments: [],
    });
    const withSttProvider = () => ({
      ...emptyDocument(),
      providers: [{ provider_id: 'stt', name: '语音识别', scope: 'owner' }],
      provider_accesses: [{
        provider_access_id: 'stt-access', provider_id: 'stt',
        enabled: true, quota_policy: {},
      }],
    });

    const result = { create: {}, remove: {}, topology: {} };

    // Enable STT with no existing provider: createProvider must run with the
    // staged CAS revision, and the reloaded authority + bumped revision must
    // land back in the settings bundle's scope (what the global 保存 then
    // sends to Api.modelRouting.replace).
    {
      registry._stgModelRouting = emptyDocument();
      registry._stgModelRoutingRevision = 7;
      let serverRevision = 7;
      let serverDocument = emptyDocument();
      const calls = [];
      window.Api = { modelRouting: {
        get: async () => ({
          model_routing: serverDocument, revision: serverRevision }),
        createProvider: async (bundle, revision) => {
          calls.push(['createProvider', revision]);
          serverRevision += 1;
          serverDocument = { ...serverDocument,
            providers: [...serverDocument.providers, bundle.provider],
            provider_accesses: [
              ...serverDocument.provider_accesses, bundle.provider_access] };
          return { ok: true };
        },
        saveProvider: async () => { throw new Error('unexpected saveProvider'); },
        deleteProvider: async () => { throw new Error('unexpected deleteProvider'); },
      } };
      document.getElementById('settingSttEnabled').checked = true;
      document.getElementById('settingSttModelOpenai').value = 'whisper-small';
      document.getElementById('settingSttBaseOpenai').value =
        'https://api.openai.com/v1';
      document.getElementById('settingSttKeyOpenai').value = 'sk-live-fixture';
      await globalThis._persistSttProvider();
      result.create = {
        calls,
        stagedRevision,
        stagedProviders: (stagedDocument.providers || [])
          .map((row) => row.provider_id),
      };
    }

    // Disable STT with an existing provider: deleteProvider must run with the
    // staged CAS revision.
    {
      registry._stgModelRouting = withSttProvider();
      registry._stgModelRoutingRevision = 11;
      let serverRevision = 11;
      const calls = [];
      window.Api = { modelRouting: {
        get: async () => ({
          model_routing: emptyDocument(), revision: serverRevision }),
        createProvider: async () => { throw new Error('unexpected createProvider'); },
        saveProvider: async () => { throw new Error('unexpected saveProvider'); },
        deleteProvider: async (providerId, revision) => {
          calls.push(['deleteProvider', providerId, revision]);
          serverRevision += 1;
          return { ok: true };
        },
      } };
      document.getElementById('settingSttEnabled').checked = false;
      await globalThis._persistSttProvider();
      result.remove = {
        calls,
        stagedRevision,
        stagedProviders: (stagedDocument.providers || [])
          .map((row) => row.provider_id),
      };
    }

    result.topology = {
      windowDocument: typeof window._stgModelRouting,
      windowRevision: typeof window._stgModelRoutingRevision,
    };
    console.log(JSON.stringify(result));
    })().catch((error) => { console.error(error); process.exitCode = 1; });
    """
)


def _run_persist_scenarios() -> dict:
    if not _node_available():
        pytest.skip("node + jsdom dev dependencies are required")
    bundle = native_module_path(
        ".native/speech.js",
        "frontend/src/features/settings/speech.ts",
    )
    proc = subprocess.run(
        ["node", "-e", _HARNESS, str(ROOT), bundle],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == 0, (
        f"speech persist harness failed:\nSTDOUT {proc.stdout}\nSTDERR {proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_persist_reads_and_reloads_through_registry_accessor_scope():
    result = _run_persist_scenarios()
    assert result["topology"] == {
        "windowDocument": "undefined",
        "windowRevision": "undefined",
    }
    assert result["create"] == {
        "calls": [["createProvider", 7]],
        "stagedRevision": 8,
        "stagedProviders": ["stt"],
    }
    assert result["remove"] == {
        "calls": [["deleteProvider", "stt", 11]],
        "stagedRevision": 12,
        "stagedProviders": [],
    }
