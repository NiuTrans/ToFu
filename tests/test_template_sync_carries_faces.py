#!/usr/bin/env python3
"""tests/test_template_sync_carries_faces.py — the sync path must not strand Claude.

THE DEFECT THIS PINS
====================
``_syncFromTemplate`` writes ONLY model-entry fields (model_id / request_ids /
capabilities / cost / aliases). It has never touched provider-level fields.
That was harmless while every model of a provider shared one wire.

It stopped being harmless when the two YourProvider templates merged: the single
template now carries 6 Claude entries that resolve to ``faces.anthropic`` BY
FAMILY, and ``_syncFromTemplate`` ADDs every template entry the provider lacks
(the ``added++`` branch). So one click of "sync from template" on an OLD card
— one with no ``faces{}`` — would land the whole Claude roster on a card that
cannot dispatch it over the Anthropic wire.

With the fail-loud resolver those entries are refused (visible). Without the
fix they would be refused on EVERY sync, i.e. the merge would have made the
sync button quietly useless for Claude. Either way the user loses, so sync
must carry the faces across.

Same hole exists in ``addProviderFromTemplate``, which copies
``extra_headers`` / ``thinking_format`` / ``protocol`` but historically not
``faces``.

Both paths are driven here as REAL source (jsdom-free: the functions are
plain data transforms, so we exercise them in node with minimal stubs).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_template_sync_carries_faces.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TA = runtime_section_path('settings/template_actions.js')

_HARNESS = r'''
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Minimal stubs — these functions are data transforms; the DOM calls are
// no-ops for our purposes.
global.window = global;
global.document = {
  querySelector: () => null, querySelectorAll: () => [],
  createElement: () => ({ style: {}, classList: { add(){} }, appendChild(){},
                          setAttribute(){}, querySelector: () => null }),
  getElementById: () => null, addEventListener(){}, removeEventListener(){},
};
global.t = (k, p) => k;
global.showAlert = () => {};
global.showConfirm = async () => true;
global._renderProvidersTab = () => {};
global._renderPresetsTab = () => {};
global._brandSvg = () => '';
global.escapeHtml = (s) => s;
global.isChatModel = () => true;
global._modelPricingCache = {};
global._stgPresets = {};
global._serverConfig = {};
global._loadExternalProviderTemplates = async () => {};
global._coldSortModels = (m) => m;
global._insertModelSorted = (arr, m) => { arr.push(m); };
global.Api = { providers: {} };

const MERGED_TEMPLATE = {
  key: 'yourprovider', brand: 'yourprovider', name: 'YourProvider (AIGC Gateway)',
  base_url: 'https://api.openai.com/v1',
  faces: { anthropic: { base_url: 'https://api.openai.com/v1/anthropic',
                        protocol: 'anthropic' } },
  extra_headers: { 'X-Custom-Header': 'your-header-value' },
  models: [
    { model_id: 'kimi-k3', capabilities: ['text'], rpm: 30, cost: 0.008 },
    { model_id: 'claude-opus-5', capabilities: ['text','vision','thinking'],
      rpm: 30, cost: 0.045, request_ids: ['yuju-claude-opus-5-evaDaily'] },
  ],
};

global._PROVIDER_TEMPLATES = [MERGED_TEMPLATE];

// Load the REAL implementation.
(0, eval)(src);

const results = {};

// ── Case 1: apply the template fresh ──
(async () => {
  global._stgProviders = [];
  await addProviderFromTemplate('yourprovider');
  const fresh = global._stgProviders[0];
  results.fresh_has_faces = !!(fresh && fresh.faces && fresh.faces.anthropic);
  results.fresh_face_url = fresh && fresh.faces && fresh.faces.anthropic
    ? fresh.faces.anthropic.base_url : null;
  results.fresh_face_proto = fresh && fresh.faces && fresh.faces.anthropic
    ? fresh.faces.anthropic.protocol : null;

  // ── Case 2: sync into an OLD faces-less card (the defect shape) ──
  global._stgProviders = [{
    id: 'example-corp', base_url: 'https://api.openai.com/v1',
    api_keys: ['k'], enabled: true,
    models: [{ model_id: 'kimi-k3', capabilities: ['text'], rpm: 30, cost: 0.008 }],
  }];
  await _syncFromTemplate(0);
  const synced = global._stgProviders[0];
  results.synced_has_faces = !!(synced.faces && synced.faces.anthropic);
  results.synced_face_proto = synced.faces && synced.faces.anthropic
    ? synced.faces.anthropic.protocol : null;
  results.synced_claude_added = (synced.models || [])
    .some((m) => m.model_id === 'claude-opus-5');

  // ── Case 3: a user-edited face must NOT be clobbered ──
  global._stgProviders = [{
    id: 'example-corp', base_url: 'https://api.openai.com/v1',
    api_keys: ['k'], enabled: true,
    faces: { anthropic: { base_url: 'https://my-proxy.internal/v1/anthropic',
                          protocol: 'anthropic' } },
    models: [],
  }];
  await _syncFromTemplate(0);
  results.user_face_preserved =
    global._stgProviders[0].faces.anthropic.base_url === 'https://my-proxy.internal/v1/anthropic';

  console.log(JSON.stringify(results));
})();
'''


def _run() -> dict:
    import shutil
    if not shutil.which('node'):
        pytest.skip('node not available')
    harness = os.path.join('/tmp', 'faces_sync_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    out = subprocess.run([shutil.which('node'), harness, _TA],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


# ═══════════════════════════════════════════════════════════

def test_applying_the_template_carries_the_wire_faces():
    """A card created from the merged template must be able to dispatch the
    Claude entries it just received."""
    r = _run()
    assert r['fresh_has_faces'], (
        'addProviderFromTemplate dropped faces{} — every Claude entry on the '
        'new card would be refused at slot-build time')
    assert r['fresh_face_proto'] == 'anthropic'
    assert '/v1/anthropic' in (r['fresh_face_url'] or '')


def test_sync_upgrades_an_old_faces_less_card():
    """THE defect: sync ADDs Claude entries but used to never add the face
    they need."""
    r = _run()
    assert r['synced_claude_added'], 'sync should add the new Claude entry'
    assert r['synced_has_faces'], (
        '_syncFromTemplate added Claude models to a card with no faces{} — '
        'those models cannot reach the Anthropic wire and are refused')
    assert r['synced_face_proto'] == 'anthropic'


def test_sync_does_not_clobber_a_user_edited_face():
    """Sync is a MERGE, not a reset — a user who repointed the face at their
    own proxy must keep it."""
    r = _run()
    assert r['user_face_preserved'], (
        'sync overwrote a user-edited face URL; it must only ADD missing faces')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
