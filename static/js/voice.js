/* ═══════════════════════════════════════════════════════════════════
   voice.js — Voice input (speech-to-text) for the chat composer.

   The "typeless" front door: click the mic → record (getUserMedia +
   MediaRecorder, webm/opus) → click again to stop → the recording is
   transcoded to WAV in the browser (the inline audio-chat path accepts only
   wav/mp3, never webm) → POSTed to /api/v1/audio/transcribe (via
   Api.audio.transcribe) → the returned text is injected at the #userInput
   cursor. It NEVER auto-sends — the user reviews /
   edits, exactly like _wrapSelectionNoTranslate.

   Graceful disable: on boot we probe Api.audio.capabilities(); the mic button
   stays hidden unless a transcription model is configured (available:true) AND
   the browser can record. So a deployment with no transcription slot never
   shows a dead affordance (CLAUDE.md §3.5).

   Bundled by lib/js_bundler.py (_BUNDLE_FILES). Symbols share window scope;
   the <script> tag in index.html is the dev-mode fallback.
   ═══════════════════════════════════════════════════════════════════ */

(function (global) {
  'use strict';

  // Recording state machine: 'idle' | 'recording' | 'transcribing'.
  var _state = 'idle';
  var _recorder = null;      // active MediaRecorder
  var _chunks = [];          // collected Blob chunks
  var _stream = null;        // active MediaStream (mic tracks)
  var _available = false;    // backend reported a transcription model
  var _models = [];          // configured transcription slots [{model, mode}]
  // Shared AudioContext, PRE-WARMED at record start so its (non-trivial)
  // construction cost is paid during the live user gesture, NOT on the
  // post-stop critical path before the transcription POST. Owned by the
  // record→idle lifecycle: created in _startRecording, reused by _encodeToWav,
  // closed on the idle transition (_applyState). A direct _encodeToWav call
  // with no prewarm (e.g. tests / programmatic use) creates+closes a transient
  // context per call, preserving the original self-contained behavior.
  var _audioCtx = null;

  function _t(key, fallback) {
    if (!key) return fallback;   // null key → use the literal fallback message
    try {
      if (typeof t === 'function') { var s = t(key); if (s && s !== key) return s; }
    } catch (e) { /* i18n not ready — use fallback */ }
    return fallback;
  }

  function _toast(kind, key, fallback) {
    try {
      if (typeof showToast === 'function') showToast(kind, _t(key, fallback));
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] toast failed:', e);
    }
  }

  /** True when the browser exposes mic capture + MediaRecorder. */
  function _browserCanRecord() {
    return !!(global.navigator && global.navigator.mediaDevices &&
              typeof global.navigator.mediaDevices.getUserMedia === 'function' &&
              typeof global.MediaRecorder !== 'undefined');
  }

  function _micBtn() { return document.getElementById('micBtn'); }

  /** Reflect the current state on the mic button (class + tooltip + disabled). */
  function _applyState(next) {
    _state = next;
    // The interaction is over — release the prewarmed AudioContext (decode has
    // already run by the time we reach 'transcribing'→'idle'). Done before the
    // btn guard so the context is freed even if the button is detached.
    if (next === 'idle') _closeAudioCtx();
    var btn = _micBtn();
    if (!btn) return;
    btn.classList.toggle('recording', next === 'recording');
    btn.classList.toggle('transcribing', next === 'transcribing');
    btn.disabled = (next === 'transcribing');
    // Honest indeterminate indicator: the CSS spin ring (.transcribing::after)
    // is a genuine indeterminate spinner (no fake progress — the gateway
    // buffers the transcript so there is nothing to stream). aria-busy makes
    // that state announceable to assistive tech.
    btn.setAttribute('aria-busy', next === 'transcribing' ? 'true' : 'false');
    var tip = next === 'recording' ? _t('voice.recording', 'Recording · click to stop')
            : next === 'transcribing' ? _t('voice.transcribing', 'Transcribing…')
            : _t('voice.tooltip', 'Voice input');
    btn.setAttribute('title', tip);
  }

  /**
   * Inject transcript text at the #userInput cursor and fire an `input` event
   * so auto-resize / send-button state update. Mirrors _wrapSelectionNoTranslate;
   * deliberately does NOT send. Inserts a leading space when joining onto
   * existing non-whitespace text so words don't glue together.
   */
  function _injectText(text) {
    var ta = document.getElementById('userInput');
    if (!ta || !text) return;
    var start = ta.selectionStart != null ? ta.selectionStart : ta.value.length;
    var end = ta.selectionEnd != null ? ta.selectionEnd : ta.value.length;
    var before = ta.value.substring(0, start);
    var after = ta.value.substring(end);
    var insert = text;
    if (before && !/\s$/.test(before) && !/^\s/.test(insert)) insert = ' ' + insert;
    ta.value = before + insert + after;
    var caret = before.length + insert.length;
    try { ta.selectionStart = ta.selectionEnd = caret; } catch (e) { /* detached node */ }
    ta.focus();
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /**
   * Return the shared AudioContext, creating it on first use. Called at record
   * START (during the user gesture) so first-use construction cost stays off
   * the post-stop critical path, and again by _encodeToWav (which reuses it).
   * Resumes a suspended context best-effort. Returns null when WebAudio is
   * unavailable (older Android WebView) — callers then fall back to the raw blob.
   */
  function _acquireAudioCtx() {
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return null;
    if (!_audioCtx) {
      try { _audioCtx = new AC(); }
      catch (e) {
        if (typeof console !== 'undefined') console.warn('[Voice] AudioContext create failed:', e);
        _audioCtx = null;
        return null;
      }
    }
    if (_audioCtx.state === 'suspended' && typeof _audioCtx.resume === 'function') {
      try { _audioCtx.resume(); } catch (e) { /* best-effort — decode still works */ }
    }
    return _audioCtx;
  }

  function _closeAudioCtx() {
    if (_audioCtx && typeof _audioCtx.close === 'function') {
      try { _audioCtx.close(); } catch (e) { /* already closed */ }
    }
    _audioCtx = null;
  }

  /**
   * Whether the recording must be transcoded to WAV before upload.
   *
   * The inline audio_chat path needs WAV (the gateway rejects webm); the
   * dedicated /audio/transcriptions endpoint (Whisper) accepts webm directly.
   * The backend tries slots in preference order and may fall back to ANY of
   * them, so we can only skip the WAV transcode when EVERY configured slot is
   * an 'endpoint' slot — if a single 'chat' slot exists it would 400 on a raw
   * webm. Unknown / empty model list → transcode (fail safe).
   */
  function _needsTranscode() {
    if (!_models.length) return true;
    return _models.some(function (m) { return (m && m.mode) !== 'endpoint'; });
  }

  function _releaseStream() {
    if (_stream) {
      try { _stream.getTracks().forEach(function (tr) { tr.stop(); }); }
      catch (e) { /* already stopped */ }
      _stream = null;
    }
  }

  /**
   * Extract a human-readable reason from a failure. The backend
   * (routes/api_v1/audio.py) sends a descriptive string in `error`
   * (envelope `.message` or a plain string) plus the HTTP status on an
   * ApiError — surface that instead of a blank "Transcription failed" so
   * the user knows WHY (no model configured, too large, upstream 5xx, …).
   */
  function _reason(src) {
    if (!src) return '';
    var err = src.body && src.body.error !== undefined ? src.body.error : src.error;
    if (err == null) err = src.message;
    if (err && typeof err === 'object') err = err.message || err.detail || err.error || '';
    return (typeof err === 'string' ? err : '').trim();
  }

  /** Toast a failure, appending the backend's reason when we have one. */
  function _failToast(reason) {
    var base = _t('voice.failed', 'Transcription failed');
    _toast('error', null, reason ? base + ': ' + reason : base);
  }

  // Map an audio MIME to the filename extension the backend allow-list keys on.
  var _EXT_FOR_TYPE = {
    'audio/wav': 'wav', 'audio/webm': 'webm', 'audio/ogg': 'ogg',
    'audio/mpeg': 'mp3', 'audio/mp4': 'm4a', 'audio/flac': 'flac',
  };
  function _extForType(type) {
    return _EXT_FOR_TYPE[(type || '').split(';')[0].trim()] || 'webm';
  }

  /**
   * Transcode a recorded blob to 16 kHz mono 16-bit WAV via WebAudio.
   *
   * WHY: browser MediaRecorder only emits webm/ogg (opus), but the inline
   * audio-chat transcription path (omni chat models, e.g. Gemini) accepts only
   * a narrow format set — wav/mp3, never webm — so a raw webm upload 400s at
   * the gateway. WAV/PCM is universally accepted (omni chat, Whisper endpoints,
   * the backend allow-list) and lets the server read the duration from the WAV
   * header. 16 kHz mono is the ASR-standard rate and keeps the payload small.
   * Falls back to the original blob when WebAudio is unavailable or decode
   * fails — endpoint/Whisper slots accept webm, so that is no regression.
   *
   * Reuses the AudioContext prewarmed at record start (so its construction
   * cost is off the post-stop critical path); a direct call with no prewarm
   * creates+closes a transient context, preserving the original behavior.
   */
  /**
   * True when post-stop transcode timing should be logged. Zero-cost in prod:
   * off unless `window.__VOICE_DEBUG` is truthy or localStorage
   * `tofu.voice.debug` is set — flip it in the WebView console to capture the
   * on-device split (arrayBuffer / acquireCtx / decodeAudioData / resample)
   * that decides whether a PCM-capture rewrite is ever justified.
   */
  function _timingOn() {
    if (global.__VOICE_DEBUG) return true;
    try { return !!(global.localStorage && global.localStorage.getItem('tofu.voice.debug')); }
    catch (e) { return false; }
  }
  function _now() {
    try { return (global.performance && global.performance.now) ? global.performance.now() : Date.now(); }
    catch (e) { return Date.now(); }
  }

  async function _encodeToWav(blob) {
    if (!blob || typeof blob.arrayBuffer !== 'function') return blob;
    // Reuse the context prewarmed by _startRecording when present; otherwise
    // create a transient one for this call (and close it in `finally`).
    var shared = !!_audioCtx;
    var dbg = _timingOn();
    var t0 = dbg ? _now() : 0, tBuf = 0, tCtx = 0, tDecode = 0, tPcm = 0;
    var ctx = _acquireAudioCtx();
    if (dbg) tCtx = _now();
    if (!ctx) return blob;
    try {
      var buf = await blob.arrayBuffer();
      if (dbg) tBuf = _now();
      var decoded = await ctx.decodeAudioData(buf);
      if (dbg) tDecode = _now();
      var wav = _audioBufferToWav(decoded, 16000);
      if (dbg) {
        tPcm = _now();
        // Note: acquireCtx≈0 when prewarmed (that's the point of prewarming).
        console.info('[Voice][timing] size=%dB shared=%s acquireCtx=%.1fms ' +
          'arrayBuffer=%.1fms decodeAudioData=%.1fms resample=%.1fms total=%.1fms',
          (blob.size || 0), shared, (tCtx - t0), (tBuf - tCtx),
          (tDecode - tBuf), (tPcm - tDecode), (tPcm - t0));
      }
      return new Blob([wav], { type: 'audio/wav' });
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] WAV encode failed, sending original:', e);
      return blob;
    } finally {
      // A prewarmed shared context is owned by the record→idle lifecycle and
      // closed on the idle transition; only close one WE created transiently.
      if (!shared) _closeAudioCtx();
    }
  }

  /**
   * Downmix an AudioBuffer to mono, linearly resample to targetRate, and encode
   * it as a 16-bit PCM WAV. Returns an ArrayBuffer.
   */
  function _audioBufferToWav(audioBuffer, targetRate) {
    var srcRate = audioBuffer.sampleRate;
    var chans = audioBuffer.numberOfChannels || 1;
    var srcLen = audioBuffer.length;
    var mono = new Float32Array(srcLen);
    for (var c = 0; c < chans; c++) {
      var data = audioBuffer.getChannelData(c);
      for (var i = 0; i < srcLen; i++) mono[i] += data[i] / chans;
    }
    var rate = targetRate || srcRate;
    var outLen = Math.max(1, Math.round(srcLen * rate / srcRate));
    var samples = new Float32Array(outLen);
    if (outLen === srcLen) {
      samples.set(mono);
    } else {
      var ratio = srcLen / outLen;
      for (var j = 0; j < outLen; j++) {
        var pos = j * ratio;
        var idx = Math.floor(pos);
        var frac = pos - idx;
        var a = mono[idx] || 0;
        var b = mono[idx + 1] != null ? mono[idx + 1] : a;
        samples[j] = a + (b - a) * frac;
      }
    }
    var bytesPerSample = 2;
    var blockAlign = bytesPerSample;      // mono
    var byteRate = rate * blockAlign;
    var dataSize = samples.length * bytesPerSample;
    var buffer = new ArrayBuffer(44 + dataSize);
    var view = new DataView(buffer);
    var writeStr = function (off, s) { for (var k = 0; k < s.length; k++) view.setUint8(off + k, s.charCodeAt(k)); };
    writeStr(0, 'RIFF');
    view.setUint32(4, 36 + dataSize, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    view.setUint32(16, 16, true);         // fmt chunk size
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, byteRate, true);
    view.setUint16(32, blockAlign, true);
    view.setUint16(34, 8 * bytesPerSample, true);
    writeStr(36, 'data');
    view.setUint32(40, dataSize, true);
    var off = 44;
    for (var m = 0; m < samples.length; m++) {
      var s = Math.max(-1, Math.min(1, samples[m]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      off += 2;
    }
    return buffer;
  }

  /** POST the recorded blob to the backend and inject the transcript. */
  async function _transcribe(blob) {
    _applyState('transcribing');
    try {
      var fd = new FormData();
      // Filename extension drives the backend MIME allow-list.
      fd.append('file', blob, 'recording.' + _extForType(blob && blob.type));
      var data = await Api.audio.transcribe(fd);
      var text = data && data.ok ? (data.text || '') : '';
      if (!data || !data.ok) {
        _failToast(_reason(data));
      } else if (!text.trim()) {
        _toast('info', 'voice.empty', 'No speech detected');
      } else {
        _injectText(text.trim());
      }
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] transcribe failed:', e);
      _failToast(_reason(e));
    } finally {
      _applyState('idle');
    }
  }

  async function _startRecording() {
    if (!_browserCanRecord()) {
      _toast('error', 'voice.noMic', 'Recording is not supported in this browser');
      return;
    }
    try {
      _stream = await global.navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] getUserMedia denied:', e);
      _toast('error', 'voice.micDenied', 'Microphone access denied');
      return;
    }
    // Pre-warm the AudioContext now, during the live user gesture, so its
    // construction cost is not paid on the post-stop critical path before the
    // transcription POST. Best-effort — recording proceeds even if it fails,
    // and _encodeToWav will lazily create one if this was skipped.
    if (_needsTranscode()) _acquireAudioCtx();
    _chunks = [];
    try {
      _recorder = new global.MediaRecorder(_stream);
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] MediaRecorder failed:', e);
      _releaseStream();
      _closeAudioCtx();   // release the just-prewarmed context — no idle transition will run
      _toast('error', 'voice.noMic', 'Recording is not supported in this browser');
      return;
    }
    _recorder.ondataavailable = function (ev) {
      if (ev && ev.data && ev.data.size > 0) _chunks.push(ev.data);
    };
    _recorder.onstop = function () {
      _releaseStream();
      var type = (_recorder && _recorder.mimeType) || 'audio/webm';
      var blob = new Blob(_chunks, { type: type });
      _chunks = [];
      if (blob.size > 0) {
        _applyState('transcribing');
        if (_needsTranscode()) {
          _encodeToWav(blob).then(function (out) { _transcribe(out); });
        } else {
          // Every configured slot is an endpoint (Whisper) slot that accepts
          // webm directly — skip the decode+resample entirely.
          _transcribe(blob);
        }
      } else _applyState('idle');
    };
    _recorder.start();
    _applyState('recording');
  }

  function _stopRecording() {
    if (_recorder && _recorder.state !== 'inactive') {
      try { _recorder.stop(); } catch (e) { _releaseStream(); _applyState('idle'); }
    } else {
      _releaseStream();
      _applyState('idle');
    }
  }

  /** Public toggle — wired to the mic button's onclick in index.html. */
  function toggleVoiceInput() {
    if (_state === 'transcribing') return;       // busy — ignore clicks
    if (_state === 'recording') _stopRecording();
    else _startRecording();
  }

  /**
   * Probe backend capability + browser support and show/hide the mic button.
   * Called once on boot. Fail-closed: any error leaves the button hidden.
   */
  async function initVoiceInput() {
    var btn = _micBtn();
    if (!btn) return;
    if (!_browserCanRecord()) { btn.style.display = 'none'; return; }
    try {
      var caps = await Api.audio.capabilities();
      _available = !!(caps && caps.available);
      _models = (caps && Array.isArray(caps.models)) ? caps.models : [];
    } catch (e) {
      if (typeof console !== 'undefined') console.warn('[Voice] capabilities probe failed:', e);
      _available = false;
    }
    btn.style.display = _available ? '' : 'none';
    if (_available) _applyState('idle');
  }

  global.toggleVoiceInput = toggleVoiceInput;
  global.initVoiceInput = initVoiceInput;
  // Testability seam (jsdom): expose internals without a behavior change.
  global.Voice = {
    _applyState: _applyState,
    _injectText: _injectText,
    _transcribe: _transcribe,
    _encodeToWav: _encodeToWav,
    _needsTranscode: _needsTranscode,
    _browserCanRecord: _browserCanRecord,
    get state() { return _state; },
    get available() { return _available; },
  };
})(typeof window !== 'undefined' ? window : this);
