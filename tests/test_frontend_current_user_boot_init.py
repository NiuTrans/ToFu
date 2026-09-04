"""Browser owner resolution is explicit, retryable, and fail closed."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/core/current-user.ts'
OWNER_JS = native_module_path('.native/current-user-contract.js', OWNER)


def test_api_and_boot_use_the_owner_probe_before_push_wiring():
    api_source = runtime_section('api.js')
    main_source = runtime_section('main.js')
    assert "me: () => get('/api/v1/users/me'" in api_source
    assert main_source.index('initCurrentUserId()') < main_source.index(
        '_wireConvSyncPush()')


def test_owner_initializer_accepts_only_authenticated_positive_owner_ids():
    source = OWNER.read_text(encoding='utf-8')
    for ambient in ('runtimeScope', 'globalThis', 'window.', 'document.', 'Api.'):
        assert ambient not in source
    script = r"""
const fs = require('fs');
let response = { authenticated: true, ownerId: 1, user: null };
let calls = 0;
const changes = [];
const logs = [];
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const identity = createCurrentUserIdentityController({
  loadCurrentUser: async () => { calls++; return response; },
  onOwnerChanged: (ownerId) => changes.push(ownerId),
  log: (message, level) => logs.push([message, level]),
});
(async () => {
  const out = {};
  out.personal = await identity.resolve();
  out.personalStored = identity.currentOwnerId();
  const afterSuccess = calls;
  response = { authenticated: true, ownerId: 99 };
  out.idempotent = await identity.resolve();
  out.idempotentCalls = calls === afterSuccess;

  identity.reset();
  response = { authenticated: false, ownerId: null };
  out.unauthenticated = await identity.resolve();

  response = null;
  out.networkFailure = await identity.resolve();

  response = { authenticated: true, ownerId: '7' };
  out.recovered = await identity.resolve();
  out.recoveredStored = identity.currentOwnerId();

  identity.reset();
  response = { authenticated: true, ownerId: 8 };
  const callsBeforeCoalesced = calls;
  out.coalesced = await Promise.all([identity.resolve(), identity.resolve()]);
  out.coalescedCalls = calls - callsBeforeCoalesced;
  out.changes = changes;
  out.levels = logs.map((entry) => entry[1]);
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    proc = subprocess.run(
        ['node', '-e', script, OWNER_JS], text=True, capture_output=True,
        check=True, timeout=30,
    )
    result = json.loads(proc.stdout)
    assert result == {
        'personal': 1,
        'personalStored': 1,
        'idempotent': 1,
        'idempotentCalls': True,
        'unauthenticated': None,
        'networkFailure': None,
        'recovered': 7,
        'recoveredStored': 7,
        'coalesced': [8, 8],
        'coalescedCalls': 1,
        'changes': [1, None, None, None, 7, None, 8],
        'levels': ['info', 'warn', 'warn', 'info', 'info'],
    }
