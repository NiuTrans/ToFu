/** Settings routing read/CAS lifecycle over the retained editor state.
 * Entry points: loadModelRoutingAuthority, persistModelRoutingNow and
 * runModelRoutingMutation and persistModelRoutingCredentialSecrets. The typed
 * picker owns committed presentation; this owner preserves unsaved editor
 * changes, credential inputs and their revision through asynchronous work.
 * API and retained state enter through the existing feature registry.
 */

import { featureRegistry } from '../../feature-registry';
import type { CredentialSecretPutResponse } from '../../api/api-v1-model-routing.generated';

interface ModelRoutingProviderRow {
  provider_id: string;
  name?: string;
  brand?: string;
}

interface ModelRoutingAccessRow {
  provider_access_id: string;
  provider_id: string;
  display_name?: string;
  enabled?: boolean;
}

export interface ModelRoutingModelRow {
  creator_id?: string;
  model_id: string;
  capabilities?: ReadonlyArray<string>;
  [key: string]: unknown;
}

interface ModelRoutingOfferingRow {
  offering_id: string;
  provider_access_id: string;
  model?: { creator_id?: string; model_id?: string };
  capabilities?: ReadonlyArray<string>;
  enabled?: boolean;
  stale?: boolean;
}

export interface ModelRoutingDocument {
  contract_version?: string;
  providers?: ReadonlyArray<ModelRoutingProviderRow>;
  provider_accesses?: ReadonlyArray<ModelRoutingAccessRow>;
  models?: ReadonlyArray<ModelRoutingModelRow>;
  offerings?: ReadonlyArray<ModelRoutingOfferingRow>;
  deployments?: ReadonlyArray<{ offering_id: string }>;
  revision?: number;
}

// A structural editor view, not a second wire schema. Publish the complete
// original response, including server controls and fields unknown here.
interface ModelRoutingResponse {
  model_routing?: ModelRoutingDocument;
  revision?: number;
}

export interface ModelRoutingAuthorityPorts {
  Api?: {
    modelRouting?: {
      get(): Promise<ModelRoutingResponse | null>;
      replace(doc: ModelRoutingDocument, revision: number): Promise<ModelRoutingResponse | null>;
      putCredentialSecret(id: string, secret: string, revision: number): Promise<CredentialSecretPutResponse | null>;
    };
  };
  debugLog?: (message: string, level?: string) => void;
  _adoptModelRoutingResponse?: (response: ModelRoutingResponse) => boolean;
  _adoptProviderCredentialDraft?: (previous: ModelRoutingDocument) => void;
  _stgModelRouting?: ModelRoutingDocument | null;
  _stgModelRoutingRevision?: number;
  _stgModelRoutingSavedJson?: string;
  _stgRoutingPersistChain?: Promise<unknown> | null;
  _stgModelRoutingLoadError?: string;
  _stgModelRoutingLoadPromise?: Promise<unknown> | null;
  _stgPendingCredentialSecrets?: Record<string, string>;
}

function globals(): ModelRoutingAuthorityPorts {
  return featureRegistry as unknown as ModelRoutingAuthorityPorts;
}

function adoptCredentialDraft(previous: ModelRoutingDocument): boolean {
  const bridge = globals();
  try {
    bridge._adoptProviderCredentialDraft?.(previous);
    return true;
  } catch (error) {
    bridge.debugLog?.('[Settings] Credential view refresh failed: ' + String(error), 'error');
    return false;
  }
}

function draftSnapshot(bridge: ModelRoutingAuthorityPorts, includeSecrets = true) {
  return {
    document: bridge._stgModelRouting,
    json: JSON.stringify(bridge._stgModelRouting),
    saved: bridge._stgModelRoutingSavedJson,
    revision: bridge._stgModelRoutingRevision,
    secrets: includeSecrets ? JSON.stringify(bridge._stgPendingCredentialSecrets || {}) : '',
  };
}

interface ReadPolicy {
  origin?: ReturnType<typeof draftSnapshot>;
  fresh?: boolean;
  minimumRevision?: number;
  keepSecrets?: boolean;
  keepDirtyDraft?: boolean;
  keepOnFailure?: boolean;
}

function draftUnchanged(origin: ReturnType<typeof draftSnapshot>): boolean {
  const bridge = globals();
  return bridge._stgModelRouting === origin.document
    && JSON.stringify(origin.document) === origin.saved
    && !Object.keys(bridge._stgPendingCredentialSecrets || {}).length;
}

function draftChangedSince(origin: ReturnType<typeof draftSnapshot>, keepSecrets = false): boolean {
  const bridge = globals();
  return bridge._stgModelRouting !== origin.document
    || JSON.stringify(bridge._stgModelRouting) !== origin.json
    || bridge._stgModelRoutingRevision !== origin.revision
    || (!keepSecrets && JSON.stringify(bridge._stgPendingCredentialSecrets || {}) !== origin.secrets);
}

/** A refresh owed to an acknowledged write is satisfied once another complete
 * adoption (e.g. the live-sync read started by that commit's wake hint) has
 * brought the editor to its revision: the editor revision only advances with
 * a committed document, so the current editor already includes the write. */
function acknowledgedRevisionHeld(policy: ReadPolicy): ModelRoutingDocument | null {
  const bridge = globals();
  if (policy.minimumRevision === undefined
      || (bridge._stgModelRoutingRevision ?? 0) < policy.minimumRevision) return null;
  return bridge._stgModelRouting || null;
}

/** Complete authority responses share one publication/adoption boundary,
 * whether received by GET or by an atomic document mutation. */
function adoptRoutingAuthority(
  response: ModelRoutingResponse | null | undefined,
  origin: ReturnType<typeof draftSnapshot>,
  policy: ReadPolicy,
  preserveDraft = false,
): ModelRoutingDocument | null {
  const bridge = globals();
  const document = response?.model_routing;
  if (!document || document.contract_version !== 'tofu.model-routing/v2') {
    throw new Error('model-routing v2 authority is unavailable');
  }
  const revision = Number(response.revision ?? document.revision ?? 0);
  if (revision < (policy.minimumRevision ?? 0)) throw new Error('model-routing refresh predates the acknowledged write');
  if (revision < (bridge._stgModelRoutingRevision ?? 0)) return acknowledgedRevisionHeld(policy);
  bridge._adoptModelRoutingResponse?.(response);
  // Composer follows committed authority. Newer editor work retains its CAS.
  if (draftChangedSince(origin, policy.keepSecrets) || (preserveDraft && !draftUnchanged(origin))
      || (policy.keepDirtyDraft && origin.json !== origin.saved)) return acknowledgedRevisionHeld(policy);
  bridge._stgModelRouting = JSON.parse(JSON.stringify(document));
  bridge._stgModelRoutingRevision = revision;
  bridge._stgModelRoutingSavedJson = JSON.stringify(bridge._stgModelRouting);
  bridge._stgModelRoutingLoadError = '';
  if (!policy.keepSecrets) bridge._stgPendingCredentialSecrets = {};
  if (policy.keepSecrets && origin.document && !adoptCredentialDraft(origin.document)) return null;
  return bridge._stgModelRouting || null;
}

function readModelRoutingAuthority(preserveDraft = false, policy: ReadPolicy = {}): Promise<unknown> {
  const bridge = globals();
  const origin = policy.origin || draftSnapshot(bridge);
  if (preserveDraft && !draftUnchanged(origin)) return Promise.resolve(null);
  // Opening Settings, refreshing the provider catalogue, and Save may all
  // need this authority at once. Share one read so a slower response cannot
  // overwrite a newer staged revision, and let a failed read be retried by
  // the next caller instead of leaving Save permanently unready.
  if (!policy.fresh && bridge._stgModelRoutingLoadPromise) return bridge._stgModelRoutingLoadPromise;
  const loadPromise = (async () => {
    try {
      const response = await bridge.Api?.modelRouting?.get();
      return adoptRoutingAuthority(response, origin, policy, preserveDraft);
    } catch (error) {
      if (policy.keepOnFailure || preserveDraft || draftChangedSince(origin, policy.keepSecrets)) {
        bridge.debugLog?.('[Settings] Probe catalog refresh failed: ' + String(error), 'error');
        return null;
      }
      bridge._stgModelRouting = null;
      bridge._stgModelRoutingLoadError = String((error as Error)?.message || error || 'unknown');
      bridge.debugLog?.('[Settings] Failed to load model-routing v2: ' + bridge._stgModelRoutingLoadError, 'error');
      return null;
    }
  })().finally(() => {
    // A synchronous client refusal can settle the async body before the
    // shared promise is assigned. Retire it in the promise continuation.
    if (bridge._stgModelRoutingLoadPromise === loadPromise) {
      bridge._stgModelRoutingLoadPromise = null;
    }
  });
  bridge._stgModelRoutingLoadPromise = loadPromise;
  return loadPromise;
}

export function loadModelRoutingAuthority(preserveDraft = false): Promise<unknown> {
  return readModelRoutingAuthority(preserveDraft);
}

/** OAuth commits arrive outside the editor queue. Read after that acknowledgement,
 * publish committed policy, and retain existing editor/credential intent just as
 * provider mutations do. An earlier in-flight opening read cannot acknowledge it.
 */
export function refreshCommittedModelRouting(): Promise<unknown> {
  return readModelRoutingAuthority(false, {
    fresh: true, keepSecrets: true, keepDirtyDraft: true, keepOnFailure: true,
  });
}

function enqueueRoutingWrite<T>(write: () => Promise<T>): Promise<T> {
  const bridge = globals();
  const run = (bridge._stgRoutingPersistChain || Promise.resolve()).catch(() => null).then(write);
  bridge._stgRoutingPersistChain = run;
  return run;
}

export interface ModelRoutingMutationReceipt {
  revision: number;
  removedCredentialIds?: ReadonlyArray<string>;
  authority?: ModelRoutingResponse;
}
export interface ModelRoutingMutationResult {
  refreshed: boolean;
}

/** Join the existing CAS chain. Partial Provider receipts require a fresh
 * projection; complete document responses enter the same adopter directly.
 * A failed refresh is
 * not a failed write: never invite replay of an acknowledged credential add.
 * Without a complete write response, newer/unsaved drafts keep their old CAS;
 * silently rebasing them could overwrite another actor's committed changes.
 */
export function runModelRoutingMutation(
  send: (revision: number) => Promise<ModelRoutingMutationReceipt | {unchanged: true}>,
  options: {persistDraft?: boolean} = {},
): Promise<ModelRoutingMutationResult> {
  return enqueueRoutingWrite(async () => {
    const bridge = globals();
    // A global Settings Save authorizes its existing draft as well as the
    // endpoint command. Keep that ordering in this queue, without rebasing a
    // dirty document from the endpoint's later partial receipt.
    if (options.persistDraft) await persistModelRoutingPass();
    if (!bridge._stgModelRouting) throw new Error('model-routing v2 authority is not loaded');
    const origin = draftSnapshot(bridge, false);
    const receipt = await send(origin.revision ?? 0);
    if (receipt && 'unchanged' in receipt && receipt.unchanged) return {refreshed: true};
    if (!receipt || !('revision' in receipt) ||
        !Number.isSafeInteger(receipt.revision) || receipt.revision <= (origin.revision ?? 0)) {
      throw new Error('model-routing mutation returned no advancing revision');
    }
    if (bridge._stgModelRouting === origin.document) {
      for (const id of receipt.removedCredentialIds || []) delete bridge._stgPendingCredentialSecrets?.[id];
    }
    const policy: ReadPolicy = {
      origin, fresh: true, minimumRevision: receipt.revision,
      keepSecrets: true, keepDirtyDraft: true, keepOnFailure: true,
    };
    if (receipt.authority) {
      try {
        return {refreshed: !!adoptRoutingAuthority(receipt.authority, origin, policy)};
      } catch (error) {
        bridge.debugLog?.('[Settings] Committed routing refresh failed: ' + String(error), 'error');
        return {refreshed: false};
      }
    }
    const refreshed = await readModelRoutingAuthority(false, policy);
    return {refreshed: !!refreshed};
  });
}

function modelRoutingDirty(): boolean {
  const bridge = globals();
  return !!bridge._stgModelRouting &&
    JSON.stringify(bridge._stgModelRouting) !== bridge._stgModelRoutingSavedJson;
}

export function hasPendingModelRoutingChanges(): boolean {
  return modelRoutingDirty() || !!Object.keys(globals()._stgPendingCredentialSecrets || {}).length;
}

/** A batch owns one queue slot and a snapshot of pending replacements. Retire
 * each acknowledged value before refreshing so a partial failure cannot replay
 * successful rotations. The receipt advances only this batch's CAS; the editor
 * needs a full response before it can adopt the new secret references safely.
 */
export function persistModelRoutingCredentialSecrets(): Promise<ModelRoutingMutationResult> {
  return enqueueRoutingWrite(async () => {
    const bridge = globals();
    const pending = bridge._stgPendingCredentialSecrets;
    const replacements = new Map(Object.entries(pending || {}));
    if (!replacements.size) return {refreshed: true};
    const origin = draftSnapshot(bridge, false);
    if (!origin.document) throw new Error('model-routing v2 authority is not loaded');
    const ownsDraft = () => bridge._stgModelRouting === origin.document
      && bridge._stgPendingCredentialSecrets === pending;
    let revision = origin.revision ?? 0;
    let refreshed = false;
    try {
      for (const [id, secret] of replacements) {
        replacements.delete(id);
        if (!ownsDraft()) break;
        if (pending?.[id] !== secret) continue;
        const receipt = await bridge.Api?.modelRouting?.putCredentialSecret(id, secret, revision);
        if (!receipt || receipt.credential_id !== id ||
            !Number.isSafeInteger(receipt.revision) || receipt.revision <= revision) {
          throw new Error('credential replacement returned an invalid receipt');
        }
        revision = receipt.revision;
        if (ownsDraft() && pending?.[id] === secret) delete pending[id];
      }
    } finally {
      replacements.clear();
      if (revision > (origin.revision ?? 0)) {
        refreshed = !!await readModelRoutingAuthority(false, {
          origin, fresh: true, minimumRevision: revision,
          keepSecrets: true, keepDirtyDraft: true, keepOnFailure: true,
        });
      }
    }
    return {refreshed};
  });
}

/* Commit staged routing edits immediately (used by model adds) instead of
 * waiting for the global Save. One pass PUTs the whole staged aggregate and
 * adopts the committed document; edits staged while that PUT is in flight
 * keep the doc dirty, so the pass loops until the staged state is fully
 * committed. Callers chain onto _stgRoutingPersistChain, never onto an
 * ad-hoc promise, so passes always run sequentially. */
async function persistModelRoutingPass(): Promise<unknown> {
  const bridge = globals();
  let passes = 0;
  while (modelRoutingDirty()) {
    if (++passes > 5) throw new Error('model-routing persist did not settle');
    const original = bridge._stgModelRouting;
    const sentJson = JSON.stringify(bridge._stgModelRouting);
    const response = await bridge.Api?.modelRouting?.replace(
      bridge._stgModelRouting as ModelRoutingDocument, bridge._stgModelRoutingRevision ?? 0);
    const committed = response && response.model_routing;
    if (!committed) throw new Error('model-routing save returned no document');
    bridge._stgModelRoutingRevision = Number(response.revision || 0);
    if (JSON.stringify(bridge._stgModelRouting) === sentJson) {
      bridge._stgModelRouting = committed;
    }
    bridge._stgModelRoutingSavedJson = JSON.stringify(committed);
    bridge._adoptModelRoutingResponse?.(response);
    if (original && bridge._stgModelRouting === committed) adoptCredentialDraft(original);
  }
  return bridge._stgModelRouting;
}


export function persistModelRoutingNow(): Promise<unknown> {
  const bridge = globals();
  if (!bridge._stgModelRouting) return Promise.resolve(null);
  return enqueueRoutingWrite(persistModelRoutingPass);
}
