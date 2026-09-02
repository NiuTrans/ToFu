/* ===== migrated source: core/conversation_catalog.js ===== */
/* Conversation catalog and metadata persistence.
 *
 * Responsibilities:
 *   - load and merge sidebar metadata;
 *   - hydrate metadata-only shells from IndexedDB;
 *   - persist conversation settings;
 *   - delegate every transcript read to ConversationTurnStore.
 *
 * Conversation Sync v3 owns every transcript Turn. This module never uploads,
 * rebases, repairs, or merges transcript content.
 */

let _lastServerLoadOk = false;
let _convMetaEtag = null;
let _conversationCatalogFlight = null;
let _serverTotalCount = null;

async function persistConversationSettings(conv) {
  if (!conv?.id) return false;
  try {
    if (!conv._localOnly) {
      const settings = (typeof _buildConvSettings === 'function')
        ? await _buildConvSettings(conv) : {};
      const response = await Api.conversations.patchSettings(conv.id, settings);
      if (!response?.ok) return false;
    }
    try {
      if (typeof ConvCache !== 'undefined') ConvCache.put(conv);
    }
    catch (error) {
      console.debug(`[conversation-catalog] cache refresh skipped: ${error?.message || error}`);
    }
    return true;
  } catch (error) {
    console.warn(`[conversation-catalog] settings sync failed for ${conv.id.slice(0, 8)}:`,
                 error?.message || error);
    return false;
  }
}

function serverLoadOk() {
  return _lastServerLoadOk;
}

function getServerTotalCount() {
  return _serverTotalCount;
}

function _catalogRevision(row) {
  return Number.isInteger(row?.rev) ? row.rev : null;
}

/** Normalize the legacy count spellings at this single catalog boundary. */
function _catalogTurnCount(row) {
  if (!row) return 0;
  const value = row.messageCount != null ? row.messageCount
    : (row.msgCount != null ? row.msgCount : row.msg_count);
  const count = Number(value);
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

function _newCatalogShell(row) {
  const count = _catalogTurnCount(row);
  const createdAt = row.createdAt || row.created_at || row.updatedAt
    || row.updated_at || row.cachedAt || Date.now();
  const shell = {
    id: row.id,
    title: row.title || 'Untitled',
    _serverTurnCount: count,
    _turnSnapshotRequired: count > 0,
    _localOnly: false,
    createdAt,
    updatedAt: row.updatedAt || row.updated_at || row.cachedAt || createdAt,
  };
  const revision = _catalogRevision(row);
  if (revision !== null) shell._serverRev = revision;
  if (typeof _applySettingsToConv === 'function') {
    _applySettingsToConv(shell, row.settings || {});
  }
  return shell;
}

function _mergeCatalogRow(local, row) {
  const previousRevision = Number.isInteger(local._serverRev)
    ? local._serverRev : null;
  const previousCount = Math.max(0, Number(local._serverTurnCount) || 0);
  const revision = _catalogRevision(row);
  const count = _catalogTurnCount(row);
  const revisionComparable = revision !== null && previousRevision !== null;
  const revisionOlder = revisionComparable && revision < previousRevision;
  const incomingUpdatedAt = Number(row.updatedAt || row.updated_at
    || row.createdAt || row.created_at || 0);
  const localUpdatedAt = Number(local.updatedAt || local.createdAt || 0);
  const metadataCurrent = !revisionOlder && (
    revisionComparable || !incomingUpdatedAt || incomingUpdatedAt >= localUpdatedAt
  );
  const bodyChanged = revisionComparable
    ? !revisionOlder && (revision > previousRevision || count !== previousCount)
    : count !== previousCount;

  local._localOnly = false;
  if (metadataCurrent) {
    local.title = row.title || local.title || 'Untitled';
    local.createdAt = row.createdAt || row.created_at || local.createdAt;
    local.updatedAt = row.updatedAt || row.updated_at || local.updatedAt;
    if (typeof _applySettingsToConv === 'function') {
      _applySettingsToConv(local, row.settings || {});
    }
  }
  /* A catalog response can arrive after a fresher Turn snapshot. Never let an
   * older revision rewind the count or re-arm hydration from stale metadata. */
  if (!revisionOlder) local._serverTurnCount = count;
  if (revision !== null
      && (previousRevision === null || revision > previousRevision)) {
    local._serverRev = revision;
  }
  if (bodyChanged) {
    local._turnSnapshotRequired = true;
    runtimeScope.ConversationTurnStore?.invalidateConversation?.(local.id);
  }
  return bodyChanged;
}

/** Merge any folder/pagination/catalog rows through the same shell rules. */
function mergeConversationCatalogRows(rows) {
  if (!Array.isArray(rows) || rows.length === 0) return 0;
  const localById = new Map(conversations.map((conversation) =>
    [conversation.id, conversation]));
  let added = 0;
  for (const row of rows) {
    if (!row?.id) continue;
    const local = localById.get(row.id);
    if (local) {
      _mergeCatalogRow(local, row);
      continue;
    }
    const shell = _newCatalogShell(row);
    conversations.push(shell);
    localById.set(shell.id, shell);
    added++;
  }
  return added;
}

/** Paint cached catalog metadata before the authoritative list arrives. */
async function hydrateConversationCatalogFromCache() {
  try {
    if (typeof ConvCache === 'undefined' || !ConvCache.isAvailable()) return 0;
    let rows = [];
    if (ConvCache.getSidebarList) rows = await ConvCache.getSidebarList();
    if (!Array.isArray(rows) || rows.length === 0) {
      rows = await ConvCache.getAllMeta();
    }
    if (!Array.isArray(rows) || rows.length === 0) return 0;
    const known = new Set(conversations.map((conversation) => conversation.id));
    let added = 0;
    for (const row of rows) {
      if (!row?.id || known.has(row.id)) continue;
      const shell = _newCatalogShell(row);
      shell._fromCache = true;
      conversations.push(shell);
      known.add(shell.id);
      added++;
    }
    if (added) {
      conversations.sort(_convSorter);
      if (typeof renderConversationList === 'function') renderConversationList();
      console.log(
        `[conversation-catalog-cache] painted ${added} cached conversation(s) before server load`,
      );
    }
    return added;
  } catch (error) {
    debugLog(
      `hydrateConversationCatalogFromCache failed: ${error?.message || error}`,
      'warn',
    );
    return 0;
  }
}

async function _fetchConversationCatalog() {
  _lastServerLoadOk = false;
  const headers = {};
  if (_convMetaEtag) headers['If-None-Match'] = _convMetaEtag;
  const makeSignal = (ms) => {
    if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
      return AbortSignal.timeout(ms);
    }
    const controller = new AbortController();
    setTimeout(() => controller.abort(), ms);
    return controller.signal;
  };

  let response = null;
  for (let attempt = 0; attempt < 3; attempt++) {
    response = await Api.conversations.listMeta({
      headers,
      signal: makeSignal(12000),
    });
    if (response?.status !== 503) break;
    const retryAfter = Number(response.headers?.get?.('Retry-After')) || attempt + 1;
    await new Promise((resolve) => setTimeout(resolve, retryAfter * 1000));
  }
  if (!response) throw new Error('Conversation catalog is unreachable.');
  if (response.status === 304) {
    _lastServerLoadOk = true;
    return null;
  }
  if (!response.ok) {
    throw new Error(`Conversation catalog failed with HTTP ${response.status}.`);
  }
  const payload = await response.json();
  if (!payload || !Array.isArray(payload.items)) {
    throw new Error('Conversation catalog returned an invalid response.');
  }
  const totalHeader = response.headers?.get?.('X-Total-Count');
  if (totalHeader !== null && totalHeader !== undefined && totalHeader !== '') {
    const parsed = Number(totalHeader);
    if (Number.isInteger(parsed) && parsed >= 0) _serverTotalCount = parsed;
  }
  _convMetaEtag = response.headers?.get?.('ETag') || null;
  _lastServerLoadOk = true;
  return payload.items;
}

async function loadConversationCatalog() {
  if (_conversationCatalogFlight) return _conversationCatalogFlight;
  _conversationCatalogFlight = (async () => {
    try {
      const rows = await _fetchConversationCatalog();
      if (rows === null) return;
      try {
        if (typeof ConvCache !== 'undefined') {
          await ConvCache.putSidebarList?.(rows);
        }
      } catch (error) {
        debugLog(`[conversation-catalog] cache write failed: ${error?.message || error}`, 'warn');
      }

      const localById = new Map(conversations.map((conversation) =>
        [conversation.id, conversation]));
      const serverIds = new Set();
      for (const row of rows) {
        if (!row?.id) continue;
        serverIds.add(row.id);
        const local = localById.get(row.id);
        if (local) _mergeCatalogRow(local, row);
        else conversations.push(_newCatalogShell(row));
      }

      const complete = Number.isInteger(_serverTotalCount)
        && _serverTotalCount <= rows.length;
      if (complete) {
        for (const conversation of [...conversations]) {
          if (conversation._localOnly || serverIds.has(conversation.id)) continue;
          if (typeof _applyRemoteConvDeleted === 'function') {
            _applyRemoteConvDeleted(conversation.id);
          }
        }
      }

      conversations.sort(_convSorter);
      renderConversationList();
    } catch (error) {
      _lastServerLoadOk = false;
      debugLog(`[conversation-catalog] ${error?.message || error}`, 'warn');
    }
  })();
  try {
    await _conversationCatalogFlight;
  } finally {
    _conversationCatalogFlight = null;
  }
}

async function hydrateConversationRuntime(convId) {
  const conv = conversations.find((conversation) => conversation?.id === convId);
  if (!conv) return null;
  const turnStore = runtimeScope.ConversationTurnStore;
  if (!turnStore?.hydrateConversation) {
    throw new Error('Conversation turn runtime is unavailable.');
  }
  await turnStore.hydrateConversation(conv);
  conv._turnSnapshotRequired = false;
  conv._serverTurnCount = runtimeScope.ConversationTurnRead?.ordered?.(conv)?.length || 0;
  if (typeof runtimeScope.Artifacts?.hydrateConversation === 'function') {
    void runtimeScope.Artifacts.hydrateConversation(conv).catch((error) =>
      console.debug('[artifacts] conversation hydrate failed:', error));
  }
  if (typeof runtimeScope.loadCompactionHistory === 'function') {
    void runtimeScope.loadCompactionHistory(convId).catch((error) =>
      console.debug('[compaction] history hydrate failed:', error));
  }
  if (typeof _retriggerHgTranslations === 'function') {
    _retriggerHgTranslations(convId);
  }
  if (convId === activeConvId && typeof _refreshServerQueue === 'function') {
    _refreshServerQueue(convId);
  }
  return conv;
}
