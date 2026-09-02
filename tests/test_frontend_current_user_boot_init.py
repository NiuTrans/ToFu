"""Browser owner resolution is explicit, retryable, and fail closed."""

from __future__ import annotations

import subprocess

import pytest

from tests._runtime_sections import runtime_section, runtime_section_path


pytestmark = pytest.mark.unit


def test_api_and_boot_use_the_owner_probe_before_push_wiring():
    api_source = runtime_section('api.js')
    main_source = runtime_section('main.js')
    assert "me: () => get('/api/v1/users/me'" in api_source
    assert main_source.index('initCurrentUserId()') < main_source.index(
        '_wireConvSyncPush()')


def test_owner_initializer_accepts_only_authenticated_positive_owner_ids():
    source = runtime_section_path('core/current_user.js')
    script = r"""
const fs = require('fs');
global.window = global;
global.debugLog = () => {};
let response = { authenticated: true, ownerId: 1, user: null };
let calls = 0;
global.Api = { users: { me: async () => { calls++; return response; } } };
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
(async () => {
  const out = {};
  out.personal = await initCurrentUserId();
  out.personalStored = window._currentUserId;
  const afterSuccess = calls;
  response = { authenticated: true, ownerId: 99 };
  out.idempotent = await initCurrentUserId();
  out.idempotentCalls = calls === afterSuccess;

  resetCurrentUserIdForTests();
  response = { authenticated: false, ownerId: null };
  out.unauthenticated = await initCurrentUserId();

  response = null;
  out.networkFailure = await initCurrentUserId();

  response = { authenticated: true, ownerId: '7' };
  out.recovered = await initCurrentUserId();
  out.recoveredStored = window._currentUserId;
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    proc = subprocess.run(
        ['node', '-e', script, source], text=True, capture_output=True,
        check=True, timeout=30,
    )
    import json
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
    }
