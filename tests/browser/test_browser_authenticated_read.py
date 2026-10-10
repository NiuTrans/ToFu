"""Browser-resident credential recipes never export tokens or follow redirects."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from lib.browser.authenticated_read import AuthenticatedRead, _READ_JSON

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('options', [
    {'origin': 'https://example.test/path'}, {'origin': 'https://user:pass@example.test'},
    {'path': '//other.test'}, {'path': '/api/../logout'},
    {'token_storage': 'arbitrary'}, {'token_storage': 'localStorage'},
    {'token_header': 'Cookie'}, {'token_prefix': 'Bearer\r\n'},
])
def test_invalid_recipe_rejected(options):
    with pytest.raises(ValueError):
        AuthenticatedRead(**{'origin': 'https://example.test', 'path': '/api/data', **options})


def test_declared_recipe_checks_policy_before_page_execution(monkeypatch):
    from lib.browser.access import BrowserAccessDenied
    calls = []
    def deny(*a, **k): raise RuntimeError('policy denied')
    monkeypatch.setattr('lib.browser.access.require_access', deny)
    page = SimpleNamespace(lease=SimpleNamespace(owner_user_id='41', client_id='a', profile=''),
                           execute=lambda *a, **k: calls.append(a))
    recipe = AuthenticatedRead('https://example.test', '/api/data', ('q',))
    with pytest.raises(ValueError, match='Undeclared'):
        recipe.execute(page, {'arbitrary': 'value'})
    with pytest.raises(RuntimeError, match='policy denied'):
        recipe.execute(page, {'q': 'value'})
    assert calls == []


@pytest.mark.parametrize('scenario', ['success', 'missing', 'origin', 'large', 'redirect', 'login'])
def test_page_request_uses_and_scrubs_token_before_result(scenario):
    harness = r'''
const fs = require('node:fs'), vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const token = 'synthetic-bearer-token';
const calls = [];
const context = vm.createContext({
 args: {origin:'https://example.test', path:'/api/data', query:{q:['hi']},
        tokenStorage:'localStorage', tokenKey:'access', tokenHeader:'Authorization', tokenPrefix:'Bearer '},
 location: {origin: input.scenario === 'origin' ? 'https://other.test' : 'https://example.test'},
 window: {localStorage: {getItem: () => input.scenario === 'missing' ? '' : token}},
 URL, AbortController, setTimeout, clearTimeout, TextDecoder,
 fetch: async (url, options) => {
   calls.push({url, cookieMode:options.credentials, redirect:options.redirect,
               headerCorrect:options.headers.Authorization === 'Bearer ' + token});
   if(input.scenario === 'redirect') throw new Error('redirect with ' + token);
   const text = JSON.stringify({title: token, nested:{access_token:token}, value:42,
                                text: input.scenario === 'large' ? 'x'.repeat(270000) : 'result'});
   let read = false;
   return {ok: true, status:input.scenario === 'login' ? 401 : 200, url,
     headers:{get:()=> 'application/json'},
     body:{getReader:()=>({read:async()=> read ? {done:true} : (read=true,{done:false,value:new TextEncoder().encode(text)}),
                          cancel:async()=>{}})}};
 }
});
vm.runInContext(input.expression, context).then(result => process.stdout.write(JSON.stringify({result,calls})));
'''
    process = subprocess.run(['node', '-e', harness], input=json.dumps({
        'expression': _READ_JSON, 'scenario': scenario}), text=True, capture_output=True, timeout=15, check=True)
    assert 'synthetic-bearer-token' not in process.stdout
    result = json.loads(process.stdout)
    if scenario == 'success':
        assert result['result']['body']['value'] == 42
        assert result['result']['body']['title'] == '[redacted]'
        assert result['calls'][0] == {'url': 'https://example.test/api/data?q=hi',
                                     'cookieMode': 'same-origin', 'redirect': 'error', 'headerCorrect': True}
    else:
        assert not result['result']['ok']
    if scenario in ('missing', 'origin'):
        assert result['calls'] == []


def test_long_nonsecret_text_does_not_amplify_redaction_work():
    # A subprocess deadline guards catastrophic regex backtracking without
    # making a microsecond wall-clock assertion on a shared test host.
    program = '''
from lib.browser.network_evidence import _redact_text
import json, sys
text = 'x' * 80000 + ' custom_access_token=synthetic-secret'
sys.stdout.write(json.dumps(_redact_text(text)))
'''
    process = subprocess.run(['python3', '-c', program], check=True, timeout=30,
                             capture_output=True, text=True)
    clean = json.loads(process.stdout)
    assert clean.startswith('x' * 80000)
    assert 'synthetic-secret' not in clean
    assert 'custom_access_token=[redacted]' in clean
