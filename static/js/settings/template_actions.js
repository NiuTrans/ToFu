/* ═══════════════════════════════════════════════════════════════════
   settings/template actions — extracted from settings.js (split 2026-05-28)

   Provider template actions: addProvider, _showTemplateMenu, _syncFromTemplate, _discoverModels, _offerTemplateUpdate.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

async function _deleteProvider(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  // A managed subscription provider (oauth marker) can't be removed by just
  // dropping the card — the login token survives on disk and any re-login /
  // token refresh re-creates it. Route to the real logout instead.
  if (p.oauth) { return _logoutManagedProvider(provIdx); }
  if (!await showConfirm(t('settings.tplDeleteConfirm', { name: (p.name || p.id), n: (p.models || []).length }), { danger: true })) return;
  _stgProviders.splice(provIdx, 1);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
}

/**
 * Remove a MANAGED subscription provider (Claude / ChatGPT OAuth) the right
 * way: log out. Deleting the card alone leaves the OAuth token on disk, so a
 * later login/refresh re-provisions it (the "why does it keep coming back?"
 * bug). Logout clears the token AND deprovisions the server_config entry, so
 * we splice the card locally to reflect that immediately.
 */
async function _logoutManagedProvider(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p || !p.oauth) return;
  var provider = p.oauth;                            // 'claude' | 'codex'
  var brandLabel = (provider === 'codex') ? 'ChatGPT' : 'Claude';
  if (!await showConfirm(t('settings.oauthLogoutConfirm', { provider: brandLabel }), { danger: true })) return;

  try {
    var r = await Api.oauth.logoutPost(provider);
    if (r && (r.status === 404 || r.status === 405)) r = await Api.oauth.logoutGet(provider);
  } catch (e) {
    showAlert(t('settings.oauthLogoutFailed', { error: e.message }));
    return;
  }

  // Reflect removal locally (server already deprovisioned) + refresh OAuth card.
  _stgProviders.splice(provIdx, 1);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
  if (typeof _updateOAuthCard === 'function') {
    _updateOAuthCard(provider, { status: 'not_started', authenticated: false });
  }
}

function addProvider() {
  var id = 'prov_' + Date.now().toString(36);
  _stgProviders.unshift({
    id: id, name: t('settings.tplNewProvider'), base_url: '', api_keys: [], enabled: true, models: []
  });
  _renderProvidersTab();
  // Expand the new provider (now first card)
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var nameInput = first.querySelector('input');
    if (nameInput) { nameInput.select(); nameInput.focus(); }
  }
}

/**
 * Show the provider template dropdown menu anchored to the button.
 * Clicking a template calls addProviderFromTemplate(key).
 */
async function _showTemplateMenu(btn) {
  // Ensure external templates are loaded before showing menu
  await _loadExternalProviderTemplates();

  // Remove any existing menu
  var existing = document.getElementById('stgTemplateMenu');
  if (existing) { existing.remove(); return; }

  var menu = document.createElement('div');
  menu.id = 'stgTemplateMenu';
  menu.className = 'stg-template-menu';

  // ── Group templates by category ──
  var _SVG = function(inner) { return '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + inner + '</svg>'; };
  var _CATEGORY_META = {
    official: { label: t('settings.tplCatOfficial'),  icon: _SVG('<path d="M10 12h4"/><path d="M10 8h4"/><path d="M14 21v-3a2 2 0 0 0-4 0v3"/><path d="M6 10H4a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-2"/><path d="M6 21V5a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16"/>'), desc: t('settings.tplCatOfficialDesc') },
    relay:    { label: t('settings.tplCatRelay'),  icon: _SVG('<path d="m18 14 4 4-4 4"/><path d="m18 2 4 4-4 4"/><path d="M2 18h1.973a4 4 0 0 0 3.3-1.7l5.454-8.6a4 4 0 0 1 3.3-1.7H22"/><path d="M2 6h1.972a4 4 0 0 1 3.6 2.2"/><path d="M22 18h-6.041a4 4 0 0 1-3.3-1.8l-.359-.45"/>'), desc: t('settings.tplCatRelayDesc') },
    _other:   { label: t('settings.tplCatOther'),      icon: _SVG('<path d="M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z"/><path d="M12 22V12"/><polyline points="3.29 7 12 12 20.71 7"/><path d="m7.5 4.27 9 5.15"/>'), desc: '' },
  };
  var _CAT_ORDER = ['official', 'relay', '_other'];
  var grouped = {};
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var cat = _PROVIDER_TEMPLATES[i].category || '_other';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(_PROVIDER_TEMPLATES[i]);
  }

  for (var ci = 0; ci < _CAT_ORDER.length; ci++) {
    var catKey = _CAT_ORDER[ci];
    var catItems = grouped[catKey];
    if (!catItems || catItems.length === 0) continue;
    var meta = _CATEGORY_META[catKey] || _CATEGORY_META._other;

    // ── Section header ──
    var header = document.createElement('div');
    header.className = 'stg-template-section';
    header.innerHTML =
      '<span class="stg-template-section-icon">' + meta.icon + '</span>' +
      '<span class="stg-template-section-label">' + meta.label + '</span>' +
      (meta.desc ? '<span class="stg-template-section-desc">' + meta.desc + '</span>' : '');
    menu.appendChild(header);

    // ── Items grid ──
    var grid = document.createElement('div');
    grid.className = 'stg-template-grid';
    for (var j = 0; j < catItems.length; j++) {
      var tpl = catItems[j];
      var item = document.createElement('div');
      item.className = 'stg-template-item';
      item.setAttribute('data-tpl-key', tpl.key);
      item.innerHTML = _brandSvg(tpl.brand, 20) +
        '<div class="stg-template-info">' +
          '<span class="stg-template-name">' + escapeHtml(tpl.name) + '</span>' +
          '<span class="stg-template-models">' + t('settings.tplModelsCount', { n: tpl.models.length }) + '</span>' +
        '</div>';
      item.onclick = (function(key) {
        return function() {
          addProviderFromTemplate(key);
          menu.remove();
        };
      })(tpl.key);
      grid.appendChild(item);
    }
    menu.appendChild(grid);
  }

  // Position below button
  btn.parentElement.style.position = 'relative';
  btn.parentElement.appendChild(menu);

  // Close on outside click
  setTimeout(function() {
    document.addEventListener('click', function _closeMenu(e) {
      if (!menu.contains(/** @type {Node} */ (e.target)) && e.target !== btn) {
        menu.remove();
        document.removeEventListener('click', _closeMenu);
      }
    });
  }, 0);
}

// ══════════════════════════════════════════════════════
//  Client-side pricing-tier tag normalization
// ══════════════════════════════════════════════════════
//
// Mirrors lib.llm_dispatch.config.reevaluate_pricing_tags() on the
// client so templates applied from _PROVIDER_TEMPLATES get correct
// 'cheap' / future-tier tags even when the hardcoded JS literal is
// stale.  Defence in depth — the static rewriter in
// debug/reeval_pricing_tags.py + CI check is still the primary
// defense; this ensures the UI never SHOWS a stale tag even if
// someone hand-edits the JS template and forgets to run the rewriter.
//
// Keeps the single threshold pair in sync with the backend:
//   'cheap': input < $3/1M AND output < $15/1M  (strict)
// Add future tiers here AND in lib/llm_dispatch/config.py::PRICING_TIERS.

/** @type {Array<[string, number, number]>} */
var _PRICING_TIERS_JS = [
  // [tag, input_max, output_max]  — per $/1M tokens
  ['cheap', 3.0, 15.0],
];
var _MANAGED_TIER_TAGS_JS = new Set(_PRICING_TIERS_JS.map(function(t) { return t[0]; }));

/**
 * Return the set of pricing-tier tags that apply to *modelId*.
 * Uses _modelPricingCache (populated from /api/server-config) for real
 * input/output prices.  Falls back to `blendedCostPer1k * 1000` against
 * the midpoint threshold when only a blended cost is available.
 *
 * Returns an Array (callers typically merge it into a capabilities set).
 */
function _getPricingTiersJS(modelId, blendedCostPer1k) {
  var mp = (typeof _modelPricingCache !== 'undefined' && _modelPricingCache)
    ? _modelPricingCache[modelId] : null;
  var inp = mp && mp.input != null ? +mp.input : null;
  var out = mp && mp.output != null ? +mp.output : null;
  var tags = [];
  for (var i = 0; i < _PRICING_TIERS_JS.length; i++) {
    var tier = _PRICING_TIERS_JS[i];
    var tag = tier[0], inMax = tier[1], outMax = tier[2];
    if (inp != null && out != null) {
      if (inp < inMax && out < outMax) tags.push(tag);
    } else if (blendedCostPer1k != null && blendedCostPer1k > 0) {
      var blended1m = blendedCostPer1k * 1000.0;
      if (blended1m <= (inMax + outMax) / 2.0) tags.push(tag);
    }
  }
  return tags;
}

/**
 * Normalize pricing-tier tags on a model dict shaped like
 * ``{model_id, capabilities, cost, ...}``.  Mutates the capabilities
 * array in place: strips stale managed tags, adds desired ones.
 * Skips non-chat models (image_gen / embedding).
 */
function _normalizeModelPricingTags(m) {
  if (!m || !m.model_id) return;
  var caps = new Set(m.capabilities || []);
  // Pricing-tier tags never apply to non-chat models. Delegate to the
  // capability taxonomy SSOT (core/model_caps.js) so a new non-chat cap
  // (e.g. 'tts') added server-side is honoured without a client rebuild.
  if (typeof isChatModel === 'function' && !isChatModel(m)) return;
  var desired = new Set(_getPricingTiersJS(m.model_id, m.cost));
  var changed = false;
  _MANAGED_TIER_TAGS_JS.forEach(function(tag) {
    if (caps.has(tag) && !desired.has(tag)) { caps.delete(tag); changed = true; }
    if (!caps.has(tag) && desired.has(tag)) { caps.add(tag); changed = true; }
  });
  if (changed) m.capabilities = Array.from(caps);
}

/**
 * Apply _normalizeModelPricingTags to every model in an array.
 */
function _normalizeModelsPricingTags(models) {
  if (!Array.isArray(models)) return;
  for (var i = 0; i < models.length; i++) _normalizeModelPricingTags(models[i]);
}

/**
 * Add a pre-configured provider from a template.
 * Pre-fills base_url and models; user just needs to add their API key.
 */
async function addProviderFromTemplate(templateKey) {
  var tpl = null;
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    if (_PROVIDER_TEMPLATES[i].key === templateKey) { tpl = _PROVIDER_TEMPLATES[i]; break; }
  }
  if (!tpl) return;

  // Check if this provider is already added
  for (var j = 0; j < _stgProviders.length; j++) {
    if (_stgProviders[j].base_url === tpl.base_url) {
      if (!await showConfirm(t('settings.tplAlreadyAdded', { name: tpl.name }))) return;
      break;
    }
  }

  var id = tpl.key + '_' + Date.now().toString(36);
  var models = tpl.models.map(function(m) {
    var entry = {
      model_id: m.model_id,
      aliases: m.aliases || [],
      capabilities: (m.capabilities || ['text']).slice(),
      rpm: m.rpm || 30,
      cost: m.cost || 0.01,
      thinking_default: (m.capabilities || []).indexOf('thinking') >= 0,
    };
    // The wire-id pool (model-identity contract). Carried verbatim so a
    // logical model_id keeps dispatching the gateway-accepted ids; dropping it
    // here would silently send the logical name and 400.
    if (m.request_ids && m.request_ids.length) entry.request_ids = m.request_ids.slice();
    return entry;
  });
  // Defense-in-depth: re-evaluate pricing-tier tags against live pricing
  // so a stale hardcoded template can't sneak an incorrect 'cheap' tag
  // (or a missing one) into a new provider card.
  _normalizeModelsPricingTags(models);
  // Order the fresh provider's models alphabetically (cold sort).
  if (typeof _coldSortModels === 'function') _coldSortModels(models);

  var newProv = {
    id: id, name: tpl.name, base_url: tpl.base_url,
    balance_url: tpl.balance_url || '',
    brand: tpl.brand || '',
    api_keys: [], enabled: true, models: models,
  };
  if (tpl.extra_headers && Object.keys(tpl.extra_headers).length > 0) {
    newProv.extra_headers = JSON.parse(JSON.stringify(tpl.extra_headers));
  }
  if (tpl.thinking_format) {
    newProv.thinking_format = tpl.thinking_format;
  }
  if (tpl.protocol) {
    newProv.protocol = tpl.protocol;
  }
  // The alternate wire faces (account/face separation). MUST be carried: the
  // template's Claude entries resolve to faces.anthropic by family, so a card
  // created without it would have every Claude model REFUSED at slot-build
  // time (lib/llm_dispatch/provider_face.py fails loud rather than silently
  // dispatching them over the signature-dropping OpenAI wire).
  if (tpl.faces && Object.keys(tpl.faces).length > 0) {
    newProv.faces = JSON.parse(JSON.stringify(tpl.faces));
  }
  _stgProviders.unshift(newProv);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);

  // Expand the new provider (now first card) and focus the API key textarea
  var first = document.querySelector('.stg-provider-card');
  if (first) {
    first.classList.add('expanded');
    first.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var textarea = first.querySelector('textarea');
    if (textarea) {
      textarea.focus();
      textarea.placeholder = t('settings.tplKeyPlaceholder', { name: tpl.name });
    }
  }
}

// ── Template Sync: merge new models from matching template into existing provider ──

/**
 * Find the matching template for a provider by base_url or brand+key.
 * Returns the template object, or null if no match.
 */
function _findMatchingTemplate(provider) {
  if (!provider) return null;
  var url = (provider.base_url || '').replace(/\/+$/, '');
  // 1. Exact base_url match
  for (var i = 0; i < _PROVIDER_TEMPLATES.length; i++) {
    var tUrl = (_PROVIDER_TEMPLATES[i].base_url || '').replace(/\/+$/, '');
    if (tUrl && url && tUrl === url) return _PROVIDER_TEMPLATES[i];
  }
  // 2. Fallback: match by brand (if explicitly set from a previous template apply)
  if (provider.brand) {
    for (var j = 0; j < _PROVIDER_TEMPLATES.length; j++) {
      if (_PROVIDER_TEMPLATES[j].brand === provider.brand || _PROVIDER_TEMPLATES[j].key === provider.brand) {
        return _PROVIDER_TEMPLATES[j];
      }
    }
  }
  return null;
}

// ── Template Sync: merge new models from template into existing provider ──

/**
 * Sync models from the matching built-in template into the provider.
 * Adds any models present in the template but missing from the provider.
 * Updates capabilities/cost for existing models if the template has newer info.
 */
async function _syncFromTemplate(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;
  await _loadExternalProviderTemplates();
  var tpl = _findMatchingTemplate(p);
  if (!tpl) {
    showAlert(t('settings.tplNoMatch'));
    return;
  }
  var added = 0;
  var updated = 0;
  var aliasesAdded = 0;

  /* ── Provider-level wire faces ──
   * Sync used to touch ONLY model entries, never provider-level fields. That
   * was safe while every model shared one wire, but the template now carries
   * Claude entries that resolve to faces.anthropic BY FAMILY. Adding those
   * entries to a card with no faces{} would make every one of them refused at
   * slot-build time (or, without the fail-loud guard, silently dispatched over
   * the wire that drops thinking-block signatures).
   *
   * So a face the template declares and the provider lacks is ADDED. Existing
   * faces are left alone — the user may have edited the URL, and this is a
   * merge, not a reset. */
  var facesAdded = 0;
  if (tpl.faces && typeof tpl.faces === 'object') {
    if (!p.faces || typeof p.faces !== 'object') p.faces = {};
    for (var fname in tpl.faces) {
      if (!Object.prototype.hasOwnProperty.call(tpl.faces, fname)) continue;
      if (!p.faces[fname]) {
        p.faces[fname] = JSON.parse(JSON.stringify(tpl.faces[fname]));
        facesAdded++;
      }
    }
  }
  // Aliases the user has on a model but which the template does NOT list.
  // These are usually hand-added and may be dead deployments — surface them
  // so the user can review and prune.
  var userOnlyAliases = [];
  var tplModels = tpl.models || [];

  /* Every id an entry answers to: its model_id + its wire pool. Mirrors
   * lib/llm_dispatch/model_entry.py::routing_group. Sync matches on this
   * rather than on model_id alone, because the model-identity contract
   * RENAMES entries: a provider card saved before the contract is keyed
   * 'yuju-claude-opus-5-evaDaily', while the template now calls that entry
   * 'claude-opus-5' with the yuju id in request_ids. Matching by model_id
   * would treat it as brand new and leave the user with TWO cards for one
   * model — both dispatching, double-counted in the picker. */
  var _entryIds = function(m) {
    var ids = m.model_id ? [m.model_id] : [];
    var pool = (m.request_ids && m.request_ids.length)
      ? m.request_ids
      : [m.model_id].concat(m.aliases || []);
    return new Set(ids.concat(pool).filter(Boolean));
  };
  var _findLocal = function(tm) {
    var want = _entryIds(tm);
    for (var k = 0; k < (p.models || []).length; k++) {
      var mine = _entryIds(p.models[k]);
      for (var id of want) { if (mine.has(id)) return k; }
    }
    return -1;
  };

  for (var i = 0; i < tplModels.length; i++) {
    var tm = tplModels[i];
    var localIdx = _findLocal(tm);
    if (localIdx >= 0) {
      // Update capabilities, cost, and the wire pool for the existing entry
      var mine = p.models[localIdx];
      var changed = false;
      if (tm.capabilities && JSON.stringify(mine.capabilities) !== JSON.stringify(tm.capabilities)) {
        mine.capabilities = tm.capabilities.slice();
        changed = true;
      }
      if (tm.cost != null && mine.cost !== tm.cost) {
        mine.cost = tm.cost;
        changed = true;
      }
      // ── Identity reconciliation ──
      // The template is authoritative about WHICH id is logical and which are
      // wire ids. Adopt its shape, but UNION the wire pool so a hand-added
      // deployment the user relies on is never silently dropped (removing a
      // working id is invisible — the remaining ones still answer).
      if (tm.request_ids && tm.request_ids.length) {
        var oldPool = (mine.request_ids && mine.request_ids.length)
          ? mine.request_ids.slice()
          : [mine.model_id].concat(mine.aliases || []).filter(Boolean);
        var tplPool = tm.request_ids.slice();
        var tplPoolSet = new Set(tplPool);
        var mergedPool = tplPool.slice();
        for (var oi = 0; oi < oldPool.length; oi++) {
          // Keep a user-only wire id, but not the pre-contract root when the
          // template has since demoted it to a logical name (that id is
          // exactly what the gateway refuses).
          if (!tplPoolSet.has(oldPool[oi]) && oldPool[oi] !== tm.model_id) {
            mergedPool.push(oldPool[oi]);
            userOnlyAliases.push(tm.model_id + ' → ' + oldPool[oi]);
          }
        }
        if (mine.model_id !== tm.model_id ||
            JSON.stringify(mine.request_ids || []) !== JSON.stringify(mergedPool)) {
          // A rename must carry the presets with it. Presets store a model_id,
          // so leaving them on the pre-contract id would silently point at a
          // model that no longer exists (the picker falls back to a default and
          // the user's chosen model is gone) — same remap _saveModelEdit does.
          if (mine.model_id && mine.model_id !== tm.model_id) {
            for (var pk in _stgPresets) {
              if (_stgPresets[pk] === mine.model_id) _stgPresets[pk] = tm.model_id;
            }
          }
          mine.model_id = tm.model_id;
          mine.request_ids = mergedPool;
          // The legacy field no longer carries routing once request_ids exists;
          // leaving it would be a second, contradictory source of truth.
          delete mine.aliases;
          aliasesAdded += mergedPool.length;
          changed = true;
        }
      } else {
        // Legacy template entry: keep the additive alias reconciliation.
        var existingAliases = (mine.aliases || []).slice();
        var existingAliasSet = new Set(existingAliases);
        var tplAliases = (tm.aliases || []);
        var tplAliasSet = new Set(tplAliases);
        for (var ai = 0; ai < tplAliases.length; ai++) {
          if (!existingAliasSet.has(tplAliases[ai])) {
            existingAliases.push(tplAliases[ai]);
            existingAliasSet.add(tplAliases[ai]);
            aliasesAdded++;
            changed = true;
          }
        }
        mine.aliases = existingAliases;
        for (var ui = 0; ui < existingAliases.length; ui++) {
          if (!tplAliasSet.has(existingAliases[ui])) {
            userOnlyAliases.push(tm.model_id + ' → ' + existingAliases[ui]);
          }
        }
      }
      if (changed) updated++;
      continue;
    }
    var _newTplModel = {
      model_id: tm.model_id,
      aliases: (tm.aliases || []).slice(),
      capabilities: (tm.capabilities || ['text']).slice(),
      rpm: tm.rpm || 30,
      cost: tm.cost || 0.01,
      thinking_default: (tm.capabilities || []).indexOf('thinking') >= 0,
    };
    if (tm.request_ids && tm.request_ids.length) {
      _newTplModel.request_ids = tm.request_ids.slice();
    }
    if (typeof _insertModelSorted === 'function') _insertModelSorted(p.models, _newTplModel);
    else p.models.push(_newTplModel);
    added++;
  }
  // Defense-in-depth: re-evaluate pricing-tier tags on the entire
  // provider after merge.  Catches both newly-added entries and any
  // existing entries whose caps were overwritten above.
  _normalizeModelsPricingTags(p.models);
  _renderProvidersTab();
  _renderPresetsTab(_serverConfig);
  var msg = t('settings.tplSyncDone');
  var parts = [];
  if (added > 0) parts.push(t('settings.tplSyncAdded', { n: added }));
  if (updated > 0) parts.push(t('settings.tplSyncUpdated', { n: updated }));
  if (aliasesAdded > 0) parts.push(t('settings.tplSyncAliases', { n: aliasesAdded }));
  if (facesAdded > 0) parts.push(t('settings.tplSyncFaces', { n: facesAdded }));
  msg += parts.length ? parts.join(t('settings.tplSyncJoin')) : t('settings.tplSyncNoChange');
  if (userOnlyAliases.length > 0) {
    msg += t('settings.tplSyncUserAliases', { list: userOnlyAliases.join('\n  • ') });
  }
  msg += t('settings.tplSyncSaveHint');
  showAlert(msg);
}

// ── Model Auto-Discovery ──

async function _discoverModels(provIdx) {
  var p = _stgProviders[provIdx];
  if (!p) return;

  var baseUrl = (p.base_url || '').trim();
  var apiKey = (p.api_keys && p.api_keys[0]) || '';

  if (!baseUrl) {
    showAlert(t('settings.tplFillUrlFirst'));
    return;
  }
  if (!apiKey) {
    showAlert(t('settings.tplFillKeyFirst'));
    return;
  }

  var modelsPath = (p.models_path || '').trim();

  // Find the discover button and show loading state
  var cards = document.querySelectorAll('.stg-provider-card');
  var card = cards[provIdx];
  var btn = card ? card.querySelector('button[onclick*="_discoverModels"]') : null;
  var oldText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = t('settings.tplDiscovering');
  }

  try {
    var data = await Api.providers.discoverModels(baseUrl, apiKey, modelsPath || '');
    if (!data) {
      showAlert(t('settings.tplDiscoverNetFail'));
      return;
    }

    if (!data.ok) {
      showAlert(t('settings.tplDiscoverFail', { error: (data.error || t('settings.tplUnknownError')) }));
      return;
    }

    var discovered = data.models || [];
    if (discovered.length === 0) {
      showAlert(t('settings.tplNoModelsFound', { url: baseUrl }));
      return;
    }

    // Merge: add only models not already present
    if (!p.models) p.models = [];
    var existing = new Set(p.models.map(function(m) { return m.model_id; }));
    var added = 0;
    for (var i = 0; i < discovered.length; i++) {
      if (!existing.has(discovered[i].model_id)) {
        if (typeof _insertModelSorted === 'function') _insertModelSorted(p.models, discovered[i]);
        else p.models.push(discovered[i]);
        existing.add(discovered[i].model_id);
        added++;
      }
    }

    _renderProvidersTab();
    // Expand the provider to show results
    var newCards = document.querySelectorAll('.stg-provider-card');
    if (newCards[provIdx]) newCards[provIdx].classList.add('expanded');

    var nCheap = discovered.filter(function(m) { return (m.capabilities || []).indexOf('cheap') >= 0; }).length;
    var _rest = (added < discovered.length)
      ? t('settings.tplDiscoverRest', { n: (discovered.length - added) })
      : t('settings.tplDiscoverRestEnd');
    var msg = t('settings.tplDiscoverResult', { total: discovered.length, cheap: nCheap, added: added, rest: _rest });
    showAlert(msg);

    // Offer to persist discovered models into the hardcoded template
    var tpl = _findMatchingTemplate(p);
    if (tpl && added > 0) {
      _offerTemplateUpdate(tpl.key, p.models);
    }

  } catch (e) {
    showAlert(t('settings.tplDiscoverError', { error: e.message }));
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }
}

// ── Persist Discovered Models into Hardcoded Template ──

/**
 * Offer to update the hardcoded provider template with the current model list.
 * Called after discovery finds new models for a template-matched provider.
 */
async function _offerTemplateUpdate(templateKey, models) {
  var ok = await showConfirm(t('settings.tplUpdateConfirm', { n: models.length }));
  if (!ok) return;

  try {
    var data = await Api.providers.updateTemplate(templateKey, models);
    if (data && data.ok) {
      showAlert(t('settings.tplUpdateDone', { n: data.model_count, files: (data.updated_files || []).join(', ') }));
    } else {
      showAlert(t('settings.tplUpdateFail', { error: (data.error || t('settings.tplUnknownError')) }));
    }
  } catch (e) {
    showAlert(t('settings.tplUpdateError', { error: e.message }));
  }
}

