#!/usr/bin/env python3
"""jsdom test for static/js/voice.js (voice input / speech-to-text).

Drives the REAL shipped voice.js under jsdom and asserts:
  * capabilities.available=false → mic button hidden; =true (+ recordable) → shown
  * a completed transcription injects the returned text at #userInput, fires the
    `input` event, and does NOT auto-send
  * the record → stop toggle walks idle→recording→(transcribe) via a fake
    MediaRecorder + getUserMedia
  * NEUTER: an {ok:false} transcription result injects NOTHING (proves the
    success-gate is load-bearing — a naive impl that injected data.text
    regardless of ok would wrongly splice failure text into the composer)

Run: make test-frontend   (skips cleanly when node/jsdom aren't installed)
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div class="input-row">' +
        '<button class="mic-btn" id="micBtn" style="display:none"></button>' +
        '<textarea id="userInput"></textarea></div></body>',
  targets: [process.argv[2]],
  globals: {
    // Api.audio surface — mutated per-test below.
    Api: { audio: { transcribe: null, capabilities: null } },
    // showToast spy.
    showToast: function () { window.__toasts = (window.__toasts || 0) + 1; },
    // send spy — voice.js must NEVER auto-send.
    _doSendOrGenerate: function () { window.__sendCalls = (window.__sendCalls || 0) + 1; },
  },
});

// Bare `Blob`/`FormData`/`Event`/`MediaRecorder` in voice.js must resolve to
// jsdom's window versions (jsdom's dispatchEvent rejects node's global Event).
global.Blob = window.Blob;
global.FormData = window.FormData;
global.Event = window.Event;

// jsdom's Blob lacks arrayBuffer(); real browsers have it. Polyfill so
// _encodeToWav's `typeof blob.arrayBuffer === 'function'` gate + read work.
if (typeof window.Blob.prototype.arrayBuffer !== 'function') {
  window.Blob.prototype.arrayBuffer = function () {
    const self = this;
    return new Promise((resolve) => {
      const fr = new window.FileReader();
      fr.onload = () => resolve(fr.result);
      fr.readAsArrayBuffer(self);
    });
  };
}

const ta = document.getElementById('userInput');
const btn = document.getElementById('micBtn');
let inputEvents = 0;
ta.addEventListener('input', function () { inputEvents++; });

// ── A fake MediaRecorder + getUserMedia so _browserCanRecord() is true ──
let getUMcalled = 0;
Object.defineProperty(window.navigator, 'mediaDevices', {
  configurable: true,
  value: { getUserMedia: async function () { getUMcalled++; return { getTracks: () => [{ stop() {} }] }; } },
});
class FakeMediaRecorder {
  constructor(stream) { this.stream = stream; this.state = 'inactive'; this.mimeType = 'audio/webm'; }
  start() { this.state = 'recording'; }
  stop() { this.state = 'inactive'; if (this.ondataavailable) this.ondataavailable({ data: new Blob(['x'], { type: 'audio/webm' }) }); if (this.onstop) this.onstop(); }
}
window.MediaRecorder = FakeMediaRecorder;

const flush = () => new Promise((r) => setImmediate(r));

(async () => {
  try {
    // ── 1. capabilities-false HIDES the mic button ──
    window.Api.audio.capabilities = async () => ({ available: false, models: [] });
    await window.initVoiceInput();
    await flush();
    check('capsFalse_hidden', btn.style.display === 'none');

    // ── 2. capabilities-true + recordable SHOWS it ──
    window.Api.audio.capabilities = async () => ({ available: true, models: [{ model: 'gpt-4o-transcribe' }] });
    await window.initVoiceInput();
    await flush();
    check('capsTrue_shown', btn.style.display === '');
    check('available_flag', window.Voice.available === true);

    // ── 3. a completed transcription injects text, fires input, no send ──
    let sentFd = null;
    window.Api.audio.transcribe = async (fd) => { sentFd = fd; return { ok: true, text: '  hello world  ' }; };
    inputEvents = 0; window.__sendCalls = 0;
    await window.Voice._transcribe(new Blob(['audio'], { type: 'audio/webm' }));
    await flush();
    check('injected_text', ta.value.indexOf('hello world') !== -1);
    check('trimmed', ta.value.trim() === 'hello world');
    check('input_event_fired', inputEvents >= 1);
    check('no_auto_send', (window.__sendCalls || 0) === 0);
    check('posted_formdata', sentFd instanceof window.FormData);
    check('state_back_to_idle', window.Voice.state === 'idle');

    // ── 4. record → stop toggle drives the state machine ──
    ta.value = '';
    window.Api.audio.transcribe = async () => ({ ok: true, text: 'second clip' });
    window.toggleVoiceInput();            // start
    await flush();
    check('recording_started', window.Voice.state === 'recording' && getUMcalled === 1);
    check('recording_class', btn.classList.contains('recording'));
    window.toggleVoiceInput();            // stop → onstop → _transcribe
    await flush(); await flush();
    check('toggle_injected', ta.value.indexOf('second clip') !== -1);

    // ── 5. NEUTER: {ok:false} must inject NOTHING (success-gate is real) ──
    ta.value = '';
    window.Api.audio.transcribe = async () => ({ ok: false, text: 'garbage should not appear' });
    await window.Voice._transcribe(new Blob(['x'], { type: 'audio/webm' }));
    await flush();
    check('neuter_no_inject_on_failure', ta.value === '');

    // ── 6. filename extension follows the blob's MIME (allow-list keys on it) ──
    // A webm blob (no AudioContext in jsdom → _encodeToWav falls back) must
    // post recording.webm; a wav blob (post-transcode) must post recording.wav.
    let name = null;
    window.Api.audio.transcribe = async (fd) => { name = fd.get('file').name; return { ok: true, text: 'x' }; };
    await window.Voice._transcribe(new Blob(['a'], { type: 'audio/webm' }));
    await flush();
    check('ext_webm', name === 'recording.webm');
    await window.Voice._transcribe(new Blob(['a'], { type: 'audio/wav' }));
    await flush();
    check('ext_wav_after_transcode', name === 'recording.wav');

    // ── 7. WebAudio present → _encodeToWav emits a RIFF/WAVE audio/wav blob ──
    window.AudioContext = class {
      decodeAudioData(_buf) {
        return Promise.resolve({
          sampleRate: 48000, numberOfChannels: 1, length: 4800,
          getChannelData: () => new Float32Array(4800),
        });
      }
      close() {}
    };
    const src = new Blob(['webmbytes'], { type: 'audio/webm' });
    const out = await window.Voice._encodeToWav(src);
    check('encode_to_wav_type', out.type === 'audio/wav');
    const head = new Uint8Array(await out.arrayBuffer());
    const tag = String.fromCharCode(head[0], head[1], head[2], head[3]) +
                String.fromCharCode(head[8], head[9], head[10], head[11]);
    check('encode_riff_wave_header', tag === 'RIFFWAVE');

    // ── 8. NEUTER: decode failure → _encodeToWav returns the ORIGINAL blob ──
    // (fallback keeps endpoint/Whisper slots working when WebAudio can't decode)
    window.AudioContext = class {
      decodeAudioData() { return Promise.reject(new Error('bad codec')); }
      close() {}
    };
    const orig = new Blob(['x'], { type: 'audio/webm' });
    const fell = await window.Voice._encodeToWav(orig);
    check('encode_fallback_on_decode_error', fell === orig);

    // ── 9. Lever 3b: _needsTranscode() reflects the configured slot modes ──
    // Re-probe capabilities with different model lists and assert the decision.
    // chat slot (audio_chat / omni) → MUST transcode (gateway 400s on webm).
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'gemini-3-flash-preview', mode: 'chat' }] });
    await window.initVoiceInput(); await flush();
    check('needsTranscode_chat', window.Voice._needsTranscode() === true);
    // all-endpoint (Whisper) slots accept webm → skip transcode.
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'gpt-4o-transcribe', mode: 'endpoint' },
               { model: 'whisper-large-v3', mode: 'endpoint' }] });
    await window.initVoiceInput(); await flush();
    check('needsTranscode_all_endpoint', window.Voice._needsTranscode() === false);
    // MIXED (one chat slot present) → MUST transcode: the backend may fall
    // back to the chat slot, which 400s on raw webm. This is the load-bearing
    // safety rule — a naive `.every(endpoint)`-style check would also be false
    // here; we require `.some(non-endpoint)` so any chat slot forces WAV.
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'gpt-4o-transcribe', mode: 'endpoint' },
               { model: 'gemini-3-flash-preview', mode: 'chat' }] });
    await window.initVoiceInput(); await flush();
    check('needsTranscode_mixed_forces_wav', window.Voice._needsTranscode() === true);
    // unknown / empty model list → fail safe to transcode.
    window.Api.audio.capabilities = async () => ({ available: true, models: [] });
    await window.initVoiceInput(); await flush();
    check('needsTranscode_empty_failsafe', window.Voice._needsTranscode() === true);

    // ── 10. all-endpoint deploy: stop() skips _encodeToWav and posts webm ──
    // Prove the transcode is actually bypassed end-to-end: the posted file
    // keeps its .webm extension (no WAV transcode) when every slot is endpoint.
    let postedName = null;
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'whisper-large-v3', mode: 'endpoint' }] });
    await window.initVoiceInput(); await flush();
    window.Api.audio.transcribe = async (fd) => { postedName = fd.get('file').name; return { ok: true, text: 'endpoint clip' }; };
    // A live AudioContext is present; if the skip were broken it would
    // transcode to WAV and post recording.wav instead.
    window.AudioContext = class {
      decodeAudioData() { return Promise.resolve({ sampleRate: 48000, numberOfChannels: 1, length: 4800, getChannelData: () => new Float32Array(4800) }); }
      close() {}
    };
    ta.value = '';
    window.toggleVoiceInput(); await flush();   // start
    window.toggleVoiceInput(); await flush(); await flush();  // stop → transcribe
    check('endpoint_skips_transcode_posts_webm', postedName === 'recording.webm');
    check('endpoint_injected', ta.value.indexOf('endpoint clip') !== -1);

    // ── 11. prewarm lifecycle: record START acquires a shared AudioContext,
    //        the idle transition closes it (no leaked context across sessions).
    let created = 0, closed = 0;
    window.AudioContext = class {
      constructor() { created++; this.state = 'running'; }
      decodeAudioData() { return Promise.resolve({ sampleRate: 48000, numberOfChannels: 1, length: 4800, getChannelData: () => new Float32Array(4800) }); }
      close() { closed++; }
    };
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'gemini-3-flash-preview', mode: 'chat' }] });
    await window.initVoiceInput(); await flush();
    window.Api.audio.transcribe = async () => ({ ok: true, text: 'warm clip' });
    ta.value = '';
    window.toggleVoiceInput(); await flush();   // start → prewarm ctx
    check('prewarm_created_ctx', created === 1);
    window.toggleVoiceInput(); await flush(); await flush();  // stop → decode(reuse) → idle → close
    check('prewarm_reused_not_recreated', created === 1);   // reused, NOT a 2nd ctx
    check('prewarm_closed_on_idle', closed === 1);
    check('prewarm_injected', ta.value.indexOf('warm clip') !== -1);

    // ── 12. NEUTER: aria-busy reflects the transcribing state honestly ──
    check('aria_busy_idle', btn.getAttribute('aria-busy') === 'false');

    // ── 13. leak guard: if MediaRecorder throws AFTER prewarm, the prewarmed
    //        AudioContext must be closed in the catch (no idle transition runs
    //        on this early-return path, so it would otherwise leak). ──
    let created2 = 0, closed2 = 0;
    window.AudioContext = class {
      constructor() { created2++; this.state = 'running'; }
      decodeAudioData() { return Promise.resolve({ sampleRate: 48000, numberOfChannels: 1, length: 4800, getChannelData: () => new Float32Array(4800) }); }
      close() { closed2++; }
    };
    const OrigMR = window.MediaRecorder;
    window.MediaRecorder = function () { throw new Error('unsupported'); };
    window.Api.audio.capabilities = async () => ({ available: true,
      models: [{ model: 'gemini-3-flash-preview', mode: 'chat' }] });
    await window.initVoiceInput(); await flush();
    window.toggleVoiceInput(); await flush();   // start → prewarm, then MR throws
    check('leak_prewarm_created', created2 === 1);
    check('leak_prewarm_closed_on_mr_fail', closed2 === 1);
    check('leak_state_idle_after_mr_fail', window.Voice.state === 'idle');
    window.MediaRecorder = OrigMR;
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_voice_input_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'voice.js'),
        body_js=_BODY,
        min_pass=32,
        label='voice-input',
    )
