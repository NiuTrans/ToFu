/* ===== migrated source: core/conversation_catalog.js ===== */
/* Conversation catalog and metadata persistence.
 *
 * Responsibilities:
 *   - adapt authoritative sidebar rows into metadata-only shells;
 *   - hydrate metadata-only shells from IndexedDB;
 *   - persist conversation settings;
 *   - delegate every transcript read to ConversationTurnStore.
 *
 * Conversation Sync v3 owns every transcript Turn. This module never uploads,
 * rebases, repairs, or merges transcript content.
 */

const _conversationSettingsResolution = createConversationSettingsResolution({
  resolveConfig: (inputs) => Api.conversations.resolveConfig(inputs),
  resolveSettings: (inputs) => Api.conversations.resolveSettings(inputs),
  patchResolvedSettings: (id, inputs) => (
    Api.conversations.patchResolvedSettings(id, inputs)
  ),
  patchSettings: (id, settings) => (
    Api.conversations.patchSettings(id, settings)
  ),
});

async function persistConversationSettings(conv) {
  if (!conv?.id) return false;
  try {
    if (!conv._localOnly) {
      const response = await _conversationSettingsResolution.persist(
        conv.id, _buildConvSettingsInputs(conv),
      );
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

function _catalogRequestSignal(timeoutMs) {
  if (typeof AbortSignal !== 'undefined' && AbortSignal.timeout) {
    return AbortSignal.timeout(timeoutMs);
  }
  const controller = new AbortController();
  setTimeout(() => controller.abort(), timeoutMs);
  return controller.signal;
}

function _applyAuthoritativeConversationCatalogRows(rows, totalCount) {
  const serverRows = rows.filter((row) => row?.id);
  const serverIds = new Set(serverRows.map((row) => row.id));
  mergeConversationCatalogRows(serverRows);

  const complete = Number.isInteger(totalCount) && totalCount <= rows.length;
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
  _wakeServerBusyConversations(serverRows);
  /* Empty storage shells are intentionally reclaimed after boot, so only
   * sidebar-visible rows participate in later 304 snapshot validation. */
  return serverRows
    .filter((row) => _catalogTurnCount(row) > 0)
    .map((row) => row.id);
}

/* The sidebar busy projection (streaming dot / "answering" tag) derives
 * exclusively from client-side Turn state. A hard refresh holds that state
 * only for hydrated conversations, so every other live conversation read
 * idle until it was opened by hand. The server stamps `busy` on catalog
 * rows whose task registry still has live work (list_running_tasks); wake
 * exactly those shells the client does not already know are busy, so
 * hydration replays the running Turns and reconnects their attempt
 * streams — the same recovery a manual open performs. */
let _serverBusyWakeInFlight = false;
function _wakeServerBusyConversations(rows) {
  if (_serverBusyWakeInFlight) return;
  const turnStore = runtimeScope.ConversationTurnStore;
  if (!turnStore?.wakeConversation) return;
  const targets = [];
  for (const row of rows || []) {
    if (!row?.busy || !row.id) continue;
    const conv = conversations.find((item) => item?.id === row.id);
    if (!conv) continue;
    if (typeof convIsBusy === 'function' && convIsBusy(conv)) continue;
    targets.push(conv);
  }
  if (!targets.length) return;
  _serverBusyWakeInFlight = true;
  runWithConcurrency(
    targets,
    (conv) => Promise.resolve(turnStore.wakeConversation(conv))
      .catch((error) => debugLog(
        `[conversation-catalog] busy wake failed for ${conv.id.slice(0, 8)}: ${error?.message || error}`,
        'warn',
      )),
    DEFAULT_ASYNC_POOL_CONCURRENCY,
  ).then(() => {
    if (typeof renderConversationList === 'function') renderConversationList();
  }).finally(() => {
    _serverBusyWakeInFlight = false;
  });
}

function _catalogContainsEveryAppliedRow(conversationIds) {
  if (conversationIds.size === 0) return true;
  const presentIds = new Set(conversations.map((conversation) => conversation?.id));
  for (const conversationId of conversationIds) {
    if (!presentIds.has(conversationId)) return false;
  }
  return true;
}

const _conversationCatalogLoader = createConversationCatalogLoader({
  requestCatalog: ({ headers, timeoutMs }) => Api.conversations.listMeta({
    headers,
    signal: _catalogRequestSignal(timeoutMs),
  }),
  applyAuthoritativeRows: _applyAuthoritativeConversationCatalogRows,
  hasEveryAppliedRow: _catalogContainsEveryAppliedRow,
  writeCache: (rows) => (
    typeof ConvCache !== 'undefined' && ConvCache.putSidebarList
      ? ConvCache.putSidebarList(rows) : Promise.resolve(0)
  ),
  wait: (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
  warn: (message) => debugLog(`[conversation-catalog] ${message}`, 'warn'),
});
retainedCompositionLifecycle.add(() => _conversationCatalogLoader.destroy());

function serverLoadOk() {
  return _conversationCatalogLoader.serverLoadOk();
}

function getServerTotalCount() {
  return _conversationCatalogLoader.serverTotalCount();
}

function loadConversationCatalog() {
  return _conversationCatalogLoader.load();
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
