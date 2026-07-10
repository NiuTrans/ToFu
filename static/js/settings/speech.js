/* ═══════════════════════════════════════════════════════════════════
   settings/speech — Speech recognition (voice-input / STT) settings tab.

   A dedicated, discoverable front door for configuring speech-to-text,
   mirroring the Machine-Translation tab pattern (other_tabs.js): an enable
   toggle, a provider selector, brand-logo cards with "get key" links, and a
   live status line from Api.audio.capabilities().

   SINGLE SOURCE OF TRUTH — no second config store. This tab is a thin editor
   over ONE dedicated provider entry (id === STT_PROVIDER_ID) inside the same
   `_stgProviders` list the general Providers tab edits and `_saveServerConfig`
   ships verbatim. The backend (lib/transcription.py) scans the built slot pool
   for the transcription/audio_chat capabilities — exactly as before. This tab
   only makes that configuration discoverable.

   ⚠️ THE DEFAULT_SLOT_CONFIGS TRAP (why we write key_access, not a plain caps
   list). At slot-build time (lib/llm_dispatch/dispatcher.py) the final slot
   capabilities come from:
       alias_cfg = DEFAULT_SLOT_CONFIGS.get(model_id)
       slot_caps = set(cell_caps) if cell_caps is not None
                   else set(alias_cfg.get('caps', model_level_caps))
   i.e. the reference-table caps OVERRIDE the model-level `capabilities` list.
   So a model that is in DEFAULT_SLOT_CONFIGS WITHOUT the audio cap (e.g. a
   generic chat name) — or a Custom model that isn't in the table at all —
   would lose transcription/audio_chat and transcription_available() would stay
   False even though the user "configured" it. Only an EXPLICIT per-(key,model)
   capability override (`key_access[keyIdx].capabilities`) wins outright. So we
   ALWAYS stamp key_access on every key index with the correct single cap. This
   is not optional — it is the whole reason the tab works.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. Registered in _BUNDLE_FILES.
   ═══════════════════════════════════════════════════════════════════ */

// The dedicated provider id this tab owns. Kept out of the general Providers
// tab's way — populate reads it, collect rewrites it, nothing else touches it.
var STT_PROVIDER_ID = 'stt';

// Per-card mechanism: OpenAI/Groq/Custom hit the multipart /audio/transcriptions
// endpoint ('transcription'); the Omni card sends audio inline via
// /chat/completions ('audio_chat'). The card sets the RIGHT cap so a user can't
// tag an omni chat model 'transcription' and 404 on a missing endpoint.
// `needsKey`: OpenAI/Groq are public cloud endpoints that CANNOT authenticate
// without a key — an enabled-but-keyless card there would build a dead slot
// (401 at request time), so the gate rejects it. Omni/Custom may legitimately
// reuse the deployment's gateway auth (e.g. Meituan AIGC) or run key-less
// locally, so a blank key is allowed — and we mark such a provider brand:'local'
// so `_build_slots_from_providers` doesn't skip it for having no API keys.
var _STT_PROVIDER_META = {
  openai: { cap: 'transcription', needsKey: true,  defaultBase: 'https://api.openai.com/v1',       defaultModel: 'gpt-4o-transcribe' },
  groq:   { cap: 'transcription', needsKey: true,  defaultBase: 'https://api.groq.com/openai/v1',  defaultModel: 'whisper-large-v3-turbo' },
  omni:   { cap: 'audio_chat',    needsKey: false, defaultBase: '',                                defaultModel: 'gemini-3-flash-preview' },
  custom: { cap: 'transcription', needsKey: false, defaultBase: '',                                defaultModel: 'whisper-1' },
};

function _sttT(key, fallback) {
  try { if (typeof t === 'function') { var s = t(key); if (s && s !== key) return s; } }
  catch (e) { /* i18n not ready */ }
  return fallback;
}

/** Find the dedicated STT provider entry in _stgProviders, or null. */
function _findSttProvider() {
  if (typeof _stgProviders === 'undefined' || !Array.isArray(_stgProviders)) return null;
  for (var i = 0; i < _stgProviders.length; i++) {
    if (_stgProviders[i] && _stgProviders[i].id === STT_PROVIDER_ID) return _stgProviders[i];
  }
  return null;
}

/**
 * Populate the Speech tab from the current config. Reads the dedicated STT
 * provider (if any) to pre-fill the matching card, sets the enable toggle from
 * the provider's `enabled` flag, and fetches live availability.
 */
function _populateSpeechTab(cfg) {
  var p = _findSttProvider();
  var enabled = !!(p && p.enabled);
  var provKind = (p && p._sttKind) || 'openai';

  var enabledCb = document.getElementById('settingSttEnabled');
  var fieldsDiv = document.getElementById('sttProviderFields');
  if (enabledCb) {
    enabledCb.checked = enabled;
    enabledCb.onchange = function () {
      if (fieldsDiv) fieldsDiv.style.display = this.checked ? '' : 'none';
    };
  }
  if (fieldsDiv) fieldsDiv.style.display = enabled ? '' : 'none';

  _setVal('settingSttProvider', provKind);

  // Pre-fill the card that matches the stored provider; leave others at their
  // HTML defaults. The stored provider carries exactly one model.
  if (p && Array.isArray(p.models) && p.models[0]) {
    var m = p.models[0];
    var key = (p.api_keys && p.api_keys[0]) || '';
    var base = p.base_url || '';
    var suffix = _sttSuffix(provKind);
    _setVal('settingSttModel' + suffix, m.model_id || '');
    _setVal('settingSttBase' + suffix, base);
    _setVal('settingSttKey' + suffix, key);
  }

  _switchSttProvider(provKind);
  _refreshSttStatus();
}

/** Capitalize the provider kind into the input-id suffix (openai→Openai). */
function _sttSuffix(kind) {
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

/** Show only the selected provider's card. */
function _switchSttProvider(kind) {
  ['openai', 'groq', 'omni', 'custom'].forEach(function (k) {
    var card = document.getElementById('sttCard' + _sttSuffix(k));
    if (card) card.style.display = (k === kind) ? '' : 'none';
  });
}

/**
 * Fetch live capability + update the status banner. Best-effort: any failure
 * leaves the banner hidden (fail-closed, matching voice.js).
 */
function _refreshSttStatus() {
  var banner = document.getElementById('sttStatusBanner');
  var text = document.getElementById('sttStatusText');
  if (!banner || !text || typeof Api === 'undefined' || !Api.audio) return;
  Api.audio.capabilities().then(function (caps) {
    if (caps && caps.available) {
      var models = (caps.models || []).map(function (m) { return m.model; }).join(', ');
      banner.style.display = '';
      text.textContent = _sttT('settings.sttStatusOn', '✓ 语音输入已就绪') +
        (models ? ' — ' + models : '');
    } else {
      banner.style.display = '';
      text.textContent = _sttT('settings.sttStatusOff',
        '语音输入未就绪 — 保存有效凭证后麦克风按钮才会出现');
    }
  }).catch(function () { banner.style.display = 'none'; });
}

/**
 * Build the dedicated STT provider object from the tab's fields, or null when
 * disabled / incomplete. The critical piece: an EXPLICIT per-(key,model)
 * `key_access` capabilities override on every key index, so the built slot
 * carries the intended cap regardless of DEFAULT_SLOT_CONFIGS (see file header).
 */
function _collectSttProvider() {
  var enabledCb = document.getElementById('settingSttEnabled');
  if (!enabledCb || !enabledCb.checked) return null;

  var kind = (document.getElementById('settingSttProvider') || {}).value || 'openai';
  var meta = _STT_PROVIDER_META[kind] || _STT_PROVIDER_META.openai;
  var suffix = _sttSuffix(kind);

  var model = ((document.getElementById('settingSttModel' + suffix) || {}).value || '').trim()
    || meta.defaultModel;
  var base = ((document.getElementById('settingSttBase' + suffix) || {}).value || '').trim()
    || meta.defaultBase;
  var key = ((document.getElementById('settingSttKey' + suffix) || {}).value || '').trim();

  // Unconfigured contract — never yield a dead slot:
  //  • no base URL       → can't route anywhere.
  //  • needsKey but blank → a public cloud endpoint (OpenAI/Groq) can't
  //    authenticate; the slot would 401 at request time. Reject it so the
  //    live status stays "not ready" instead of "configured but broken".
  if (!model || !base) return null;
  if (meta.needsKey && !key) return null;

  var api_keys = key ? [key] : [];
  // A key-less provider (omni/custom reusing gateway/local auth) MUST be
  // brand:'local' or `_build_slots_from_providers` skips it for having no
  // keys — the same silent "configured but no mic" failure class as the caps
  // trap. When keyed, brand stays '' (a normal cloud provider).
  var isLocal = api_keys.length === 0;
  // key_access must cover EVERY key index: the single real key, or index 0 of
  // the synthetic blank-key slot the dispatcher builds for a local provider.
  var nKeys = api_keys.length || 1;
  var key_access = {};
  for (var i = 0; i < nKeys; i++) {
    key_access[String(i)] = { capabilities: [meta.cap] };
  }

  var prov = {
    id: STT_PROVIDER_ID,
    name: _sttT('settings.sttService', '语音识别'),
    _sttKind: kind,               // remembered so populate re-selects the card
    base_url: base,
    api_keys: api_keys,
    enabled: true,
    // brand 'local' lets a key-less gateway/local endpoint still build a slot.
    brand: isLocal ? 'local' : '',
    models: [{
      model_id: model,
      aliases: [],
      // Model-level caps are a hint; key_access is what actually wins at
      // slot-build (see header). Set both so the model card also reads right.
      capabilities: [meta.cap],
      key_access: key_access,
      rpm: 60,
      cost: 0.001,
    }],
  };
  return prov;
}

/**
 * Mutate `_stgProviders` so it contains exactly the current tab state: remove
 * any prior STT provider, then append the freshly collected one (if enabled &
 * complete). Called from _saveServerConfig BEFORE the payload is shipped.
 */
function _applySttToProviders() {
  if (typeof _stgProviders === 'undefined' || !Array.isArray(_stgProviders)) return;
  // Drop any existing dedicated STT provider.
  for (var i = _stgProviders.length - 1; i >= 0; i--) {
    if (_stgProviders[i] && _stgProviders[i].id === STT_PROVIDER_ID) {
      _stgProviders.splice(i, 1);
    }
  }
  var prov = _collectSttProvider();
  if (prov) _stgProviders.push(prov);
}
