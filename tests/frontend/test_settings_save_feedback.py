"""Settings saves expose pending/failure state through the actual typed owners."""
import json
import os
import re
import subprocess
from pathlib import Path
import pytest
from tests._runtime_sections import _require_frontend_bundler

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = str(ROOT)
SETTINGS_CSS = ROOT / 'frontend/src/styles/application/07-settings-providers-onboarding.css'

def _read(path):
    return Path(path).read_text()

@pytest.fixture(scope='module')
def settings_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp('settings-feedback')
    entry = directory / 'entry.ts'
    entry.write_text('\n'.join([
        'export { featureRegistry as bridge } from ' + json.dumps(str(ROOT/'frontend/src/feature-registry')) + ';',
        'export { t } from ' + json.dumps(str(ROOT/'frontend/src/i18n')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/core-panel')) + ';',
        'import ' + json.dumps(str(ROOT/'frontend/src/features/settings/save-export')) + ';',
    ]))
    output = directory / 'settings.cjs'
    # Shared dep guard: skip on dep-less lanes, fail under
    # TOFU_REQUIRE_FRONTEND=1 (was an unguarded bundler call).
    _require_frontend_bundler()
    result = subprocess.run(['node','scripts/vite_test_bundle.mjs',str(entry),
        '--bundle','--platform=node','--format=cjs',f'--outfile={output}'],
        cwd=ROOT,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr
    return output

@pytest.mark.parametrize('scenario', ['feature_pending','feature_failure','collector_failure',
    'busy_authority','server_failure','success_hint','authority_retry'])
def test_settings_save_feedback_behavior(settings_bundle, scenario):
    harness = r'''
const assert=require('node:assert/strict');
global.window=globalThis;
global.localStorage={getItem:()=>null,setItem:()=>{}};
global.HTMLInputElement=class {value='';checked=false;};
global.HTMLSelectElement=class extends HTMLInputElement {};
global.HTMLTextAreaElement=class extends HTMLInputElement {};
global.HTMLButtonElement=class {disabled=false;};
const button=new HTMLButtonElement(),hint={textContent:''};let closed=0;
const elements={settingsSaveBtn:button,settingsStatusHint:hint,
 settingsModal:{classList:{remove:()=>closed++}}};
global.document={getElementById:(id)=>elements[id]||null,documentElement:{lang:'en'}};
const {bridge,t}=require(BUILT);
bridge.config={};bridge._featureFlags={};bridge._registeredModels=[];
bridge._configForPersist=()=>({});bridge.debugLog=()=>{};
let requests=0;
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
function serverReady(){
 bridge._serverConfig={};bridge._stgModelRouting={contract_version:'tofu.model-routing/v2'};
 bridge._persistModelRoutingNow=async()=>{};
 bridge.Api={serverConfig:{update:async()=>({json:async()=>({ok:true})})}};
}
async function main(){
 if(SCENARIO==='feature_pending'){
  elements.settingTradingEnabled=Object.assign(new HTMLInputElement(),{checked:true});
  const gate=defer();bridge.Api={features:{set:()=>{requests++;return gate.promise;}}};
  const saving=bridge.saveSettings();await tick();
  assert.equal(button.disabled,true,'save stays busy until the feature write is acknowledged');
  assert.equal(closed,0);await bridge.saveSettings();assert.equal(requests,1);
  gate.resolve({json:async()=>({ok:true})});await saving;
  assert.equal(button.disabled,false);assert.equal(closed,1);
  assert.equal(bridge._featureFlags.trading_enabled,true);
 } else if(SCENARIO==='feature_failure'){
  elements.settingTradingEnabled=Object.assign(new HTMLInputElement(),{checked:true});
  bridge.Api={features:{set:async()=>({json:async()=>({ok:false,error:'denied'})})}};
  await bridge.saveSettings();await tick();
  assert.equal(closed,0,'failed feature writes must not close Settings as saved');
  assert.match(hint.textContent,/保存失败/);assert.equal(button.disabled,false);
  assert.notEqual(bridge._featureFlags.trading_enabled,true);
 } else if(SCENARIO==='collector_failure'){
  bridge._collectResponsesExperimentControls=()=>{throw new Error('collector failed');};
  await bridge.saveSettings();assert.equal(button.disabled,false);
  assert.match(hint.textContent,/collector failed/);assert.equal(closed,0);
 } else if(SCENARIO==='busy_authority'){
  serverReady();bridge._stgModelRouting=null;
  const gate=defer();let speechWrites=0;
  bridge._loadModelRoutingAuthority=()=>{requests++;return gate.promise;};
  bridge._persistSttProvider=async()=>{speechWrites++;throw new Error('speech failed');};
  const saving=bridge.saveSettings();await tick();
  assert.equal(button.disabled,true);assert.equal(hint.textContent,t('common.saving'));
  await bridge.saveSettings();assert.equal(requests,1);assert.equal(speechWrites,0);
  gate.resolve({});await saving;
  assert.equal(speechWrites,1);assert.equal(button.disabled,false);
  assert.match(hint.textContent,/speech failed/);assert.equal(closed,0);
 } else if(SCENARIO==='server_failure'){
  serverReady();bridge.Api.serverConfig.update=async()=>{throw new Error('server failed');};
  await bridge.saveSettings();assert.equal(button.disabled,false);
  assert.match(hint.textContent,/server failed/);assert.equal(closed,0);
 } else if(SCENARIO==='success_hint'){
  serverReady();global.setTimeout=()=>1;
  await bridge.saveSettings();assert.equal(button.disabled,false);
  assert.equal(hint.textContent,t('settings.saved'));assert.equal(closed,1);
 } else if(SCENARIO==='authority_retry'){
  const gate=defer();bridge.Api={modelRouting:{get:()=>{requests++;return gate.promise;}}};
  const first=bridge._loadModelRoutingAuthority(),second=bridge._loadModelRoutingAuthority();
  assert.equal(first,second);assert.equal(requests,1);
  gate.reject(new Error('temporary outage'));assert.equal(await first,null);
  assert.equal(bridge._stgModelRoutingLoadPromise,null);
  bridge.Api.modelRouting.get=async()=>({revision:7,model_routing:{contract_version:'tofu.model-routing/v2'}});
  assert.ok(await bridge._loadModelRoutingAuthority());
  assert.equal(bridge._stgModelRoutingRevision,7);assert.equal(bridge._stgModelRoutingLoadError,'');
 }
 console.log('SETTINGS_FEEDBACK_COMPLETE');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
'''
    harness=harness.replace('BUILT',json.dumps(str(settings_bundle))).replace('SCENARIO',json.dumps(scenario))
    result=subprocess.run(['node','-e',harness],cwd=ROOT,capture_output=True,text=True,timeout=20)
    assert result.returncode == 0, result.stdout+result.stderr
    assert 'SETTINGS_FEEDBACK_COMPLETE' in result.stdout


def test_footer_save_button_has_stable_id():
    """The busy latch looks the button up by id; the id must stay on the
    footer save button in index.html."""
    html = _read(os.path.join(PROJECT_ROOT, 'index.html'))
    match = re.search(r'<button[^>]*data-tofu-action="saveSettings\(\)"[^>]*>', html)
    assert match, 'footer save button with data-tofu-action="saveSettings()" not found'
    assert 'id="settingsSaveBtn"' in match.group(0), (
        'footer save button lost id="settingsSaveBtn" — the busy latch would '
        'silently stop disabling it.'
    )

def test_footer_button_disabled_style_exists():
    css = _read(SETTINGS_CSS)
    assert re.search(r'\.settings-footer \.btn:disabled', css), (
        'disabled style for settings-footer buttons missing from '
        '07-settings-providers-onboarding.css.'
    )

def test_common_saving_key_in_locales():
    for lang in ('zh', 'en'):
        data = json.loads(_read(os.path.join(
            PROJECT_ROOT, 'frontend', 'src', 'i18n', 'locales', f'{lang}.json')))
        assert data.get('common.saving'), f'common.saving missing from {lang}.json'
