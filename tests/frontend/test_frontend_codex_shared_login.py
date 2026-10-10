"""Codex reuse transport preserves proxy fallback and typed recovery reasons."""
import json
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def reuse_bundle(tmp_path_factory):
    built = tmp_path_factory.mktemp('codex-reuse') / 'reuse.cjs'
    compiled = subprocess.run([
        str(ROOT / 'scripts/vite_test_bundle.mjs'),
        str(ROOT / 'frontend/src/api/codex-shared-login.ts'), '--bundle',
        '--format=cjs', '--platform=node', f'--outfile={built}',
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    return built


@pytest.mark.parametrize('first_status,second_status,issue', [
    (200, None, ''), (404, 200, ''), (405, 200, ''),
    (400, None, 'codex_access_expired'), (405, 400, 'codex_access_expired'),
    (401, None, ''), (403, None, ''), (500, None, ''),
    (400, None, ''), (0, None, ''), (404, 403, ''),
])
def test_reuse_keeps_restriction_and_failure_channels(
        reuse_bundle, first_status, second_status, issue):
    harness = r'''
global.window = global;
global.TOFU_API_V4 = true;
global.location = {pathname: '/proxy/15000/'};
global.document = {getElementById: () => null};
global.sessionStorage = {getItem: () => null, setItem: () => {}};
const calls = [];
const statuses = STATUSES;
global.fetch = async (url, options) => {
  calls.push({url, method: options.method, body: options.body});
  const status = statuses.shift();
  if (!status) throw new TypeError('Synthetic network failure');
  const body = status === 200
    ? {status: 'success', credential_source: 'local_codex'}
    : {status: 'error', auth_issue: ISSUE, error: 'Redacted login failure'};
  return new Response(JSON.stringify(body), {
    status, headers: {'Content-Type': 'application/json'},
  });
};
const {reuseCodexLogin} = require(BUNDLE);
reuseCodexLogin().then(result => {
  console.log(JSON.stringify({result, calls}));
}).catch(error => {
  console.log(JSON.stringify({error: {status: error.status}, calls}));
});
'''.replace('STATUSES', json.dumps([first_status, second_status])).replace(
        'ISSUE', json.dumps(issue)).replace('BUNDLE', json.dumps(str(reuse_bundle)))
    run = subprocess.run(['node', '-e', harness], cwd=ROOT,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    output = json.loads(run.stdout.strip().splitlines()[-1])
    calls = output['calls']
    assert len(calls) == (2 if second_status is not None else 1)
    assert calls[0]['url'] == '/proxy/15000/api/oauth/login'
    assert calls[0]['method'] == 'POST'
    assert json.loads(calls[0]['body']) == {'provider': 'codex', 'reuse_only': True}
    if second_status is not None:
        assert calls[1]['method'] == 'GET'
        assert calls[1]['url'] == '/proxy/15000/api/oauth/login?provider=codex&reuse_only=true'
        assert calls[1].get('body') is None
    terminal_status = second_status if second_status is not None else first_status
    if terminal_status == 200:
        assert output['result'] == {'status': 'success', 'credential_source': 'local_codex'}
    elif terminal_status == 400 and issue:
        assert output['result'] == {'status': 'error', 'auth_issue': issue}
    else:
        assert output['error']['status'] == terminal_status
