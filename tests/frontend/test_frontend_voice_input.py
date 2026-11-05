"""Composer dictation behavior through the native voice entry point."""
import pytest
from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path, native_module_graph

pytestmark = pytest.mark.unit

_LIFETIME = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<button id="micBtn"></button><textarea id="userInput"></textarea>',
  globals: {Api: {audio: {}}, showToast(text, kind) {window.__toast = {text, kind};},
    _frameIsOurs: owner => owner === 7,
    pushSubscribe(channel, task, callback) {window.__subscription = callback;},
    pushUnsubscribe(channel, task, callback) {check('push listener released', callback === window.__subscription);},
    pushOnReconnect(callback) {window.__reconnect = callback; return () => {window.__reconnect = null;};},
    _doSendOrGenerate() {window.__autoSent = true;},
  },
});
global.Blob = window.Blob; global.FormData = window.FormData; global.Event = window.Event;
let requests = 0, stopped = 0, posts = 0, reads = 0, available = true, failStart = false, sent = null;
const permissions = [];
Object.defineProperty(window.navigator, 'mediaDevices', {value: {
  getUserMedia: () => {requests++; return new Promise(resolve => permissions.push(resolve));},
}});
window.MediaRecorder = class {
  state = 'inactive'; mimeType = 'audio/webm';
  start() { if (failStart) throw Error('recorder start failed'); this.state = 'recording'; }
  stop() { this.state = 'inactive'; this.ondataavailable?.({data: new Blob(['clip'])}); this.onstop?.(); }
};
window.Api.audio.capabilities = async () => {reads++; return {available, models: [{mode:'endpoint'}]};};
window.Api.audio.transcribe = async (data) => {posts++; sent = data; return {ok:true,text:'  dictated  '};};
const tick = () => new Promise(resolve => setImmediate(resolve));
const allow = () => permissions.splice(0).forEach(resolve => resolve({getTracks: () => [{stop(){stopped++;}}]}));
const button = document.getElementById('micBtn');
(async () => {
  await initVoiceInput();
  if (SCENARIO === 'pending_permission') {
    toggleVoiceInput(); toggleVoiceInput(); await tick();
    check('one permission request per recording', requests === 1);
    allow(); await tick(); toggleVoiceInput(); await tick();
    check('one recording is transcribed', posts === 1);
  } else if (SCENARIO === 'refresh_recording') {
    toggleVoiceInput(); await tick(); allow(); await tick();
    available = false; await initVoiceInput();
    check('refresh preserves the stop action', button.classList.contains('recording') && button.style.display !== 'none');
    toggleVoiceInput(); await tick();
    check('original recording settles', stopped === 1 && posts === 1);
    check('unavailable microphone hides after settlement', button.style.display === 'none');
  } else if (SCENARIO === 'start_failure') {
    failStart = true; toggleVoiceInput(); await tick(); allow(); await tick();
    check('start failure releases acquired stream', stopped === 1);
    check('failed recording never posts', posts === 0);
  } else if (SCENARIO === 'push') {
    const initial = reads; available = false;
    window.__subscription({type:'model_routing.changed', userId:8, rev:12}); await tick();
    check('foreign hint ignored', reads === initial);
    window.__subscription({type:'model_routing.changed', userId:7, rev:12}); await tick();
    check('owned hint refreshes button', reads === initial + 1 && button.style.display === 'none');
    window.__subscription({type:'model_routing.changed', userId:7, rev:12}); await tick();
    check('duplicate revision needs no read', reads === initial + 1);
    available = true; window.__reconnect(); await tick();
    check('reconnect repairs missed hint', button.style.display === '');
    destroyVoiceInput(); check('reconnect listener released', window.__reconnect === null);
  } else if (SCENARIO === 'retired_permission') {
    toggleVoiceInput(); await tick(); available = false; await initVoiceInput();
    allow(); await tick();
    check('late grant is released', stopped === 1);
    check('revoked pending recording does not post', posts === 0 && !button.classList.contains('recording'));
  } else if (SCENARIO === 'unsupported') {
    delete window.MediaRecorder; await initVoiceInput();
    check('unsupported browser hides microphone', button.style.display === 'none');
    await toggleVoiceInput(); check('unsupported action explains why', window.__toast.kind === 'error');
  } else {
    const input = document.getElementById('userInput');
    input.value = 'before replace after'; input.setSelectionRange(7,14);
    let changes = 0; input.addEventListener('input', () => changes++);
    if (SCENARIO === 'failure') window.Api.audio.transcribe = async () => ({ok:false,text:'do not inject',error:{message:'synthetic refusal'}});
    if (SCENARIO === 'empty') window.Api.audio.transcribe = async () => ({ok:true,text:'   '});
    toggleVoiceInput(); await tick(); allow(); await tick(); toggleVoiceInput(); await tick();
    if (SCENARIO === 'success') {
      check('trimmed transcript replaces selection', input.value === 'before dictated after');
      check('native input event updates the composer', changes === 1);
      check('file extension matches recorded MIME', sent.get('file').name === 'recording.webm');
    } else {
      check('unsuccessful result preserves typed draft', input.value === 'before replace after' && changes === 0);
      check('failure is visible', SCENARIO === 'empty' ? window.__toast.kind === 'info' : window.__toast.text.includes('synthetic refusal'));
    }
    check('settlement clears busy state', button.getAttribute('aria-busy') === 'false' && !button.disabled);
    check('dictation never auto-sends', window.__autoSent !== true);
  }
  report();
})().catch(error => {check(String(error),false);report();});
'''


@pytest.mark.parametrize('scenario', ['pending_permission', 'refresh_recording', 'start_failure',
    'push', 'retired_permission', 'unsupported', 'success', 'failure', 'empty'])
def test_voice_recording_lifetime(scenario):
    run_harness(
        target_js=native_module_path('.native/voice-input.js', 'frontend/src/features/voice-input.ts'),
        body_js=_LIFETIME.replace('SCENARIO', repr(scenario)),
        min_pass=2,
        label=f'voice-lifetime-{scenario}',
    )


def test_speech_panel_and_composer_share_live_capabilities():
    run_harness(
        target_js=native_module_graph([
            ('.native/voice-input.js', 'frontend/src/features/voice-input.ts'),
            ('.native/speech.js', 'frontend/src/features/settings/speech.ts'),
        ]),
        body_js=r'''
const {setup} = require(process.env.JSDOM_HARNESS);
const {window, document, check, report} = setup({
  root: process.argv[3], targets: [process.argv[2]],
  html: '<button id="micBtn"></button><textarea id="userInput"></textarea>' +
    '<div id="sttStatusBanner"><span id="sttStatusText"></span></div>',
  globals: {Api:{audio:{}}, _frameIsOurs: () => true,
    pushSubscribe(channel, task, callback) {window.__notify = callback;}, pushUnsubscribe() {},
  },
});
const tick = () => new Promise(resolve => setImmediate(resolve));
let reads = 0, available = true;
for (const name of ['HTMLElement', 'HTMLInputElement', 'HTMLSelectElement']) global[name] = window[name];
Object.defineProperty(window.navigator, 'mediaDevices', {value:{getUserMedia:async()=>{}}});
window.MediaRecorder = class {};
window.Api.audio.capabilities = async () => {reads++;return {available,models:[{model:'fixture-model'}]};};
(async () => {
  populateSpeechTab(); await initVoiceInput(); await tick();
  check('panel and composer share one request', reads === 1);
  const text = document.getElementById('sttStatusText');
  check('panel projects server models', text.textContent.includes('fixture-model'));
  available = false; window.__notify({type:'model_routing.changed',userId:7,rev:2}); await tick();
  check('both views update after hint', document.getElementById('micBtn').style.display === 'none' && !text.textContent.includes('fixture-model'));
  destroySpeechTab(); text.textContent = 'retired';
  available = true; window.__notify({type:'model_routing.changed',userId:7,rev:3}); await tick();
  check('retired panel cannot repaint', text.textContent === 'retired');
  check('live composer still updates', document.getElementById('micBtn').style.display === '');
  destroyVoiceInput(); report();
})().catch(error => {check(String(error),false);report();});
''',
        min_pass=5,
        label='shared-speech-capabilities',
    )
