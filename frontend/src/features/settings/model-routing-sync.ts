/*
 * Model-routing live sync — hot updates for the Settings model panel.
 *
 * Responsibility: subscribe to the owner-scoped ``model_routing.changed``
 * wake hint (PUSH_FRAME_SPECS in lib/agent_core/events.py, emitted by every
 * successful model-routing v2 aggregate commit), reconcile the staged v2
 * authority over HTTP, and repaint the model-routing settings tabs. Entry
 * point: module evaluation wires one idempotent push subscription; the
 * settings feature loads lazily, so the push multiplexer is already up.
 * Dependencies: the staged-authority bridge (settings.js accessors plus the
 * core-panel.ts loader), frame narrowing (core/frame-identity.ts), and the
 * retained push transport. The frame is a hint only — GET
 * /api/v4/model-routing remains the single authority, so a lost hint is
 * repaired by the next settings open or push-reconnect revalidation.
 */

import { featureRegistry } from '../../feature-registry';
import { narrowModelRoutingChangedFrame } from '../../core/frame-identity';

interface ModelRoutingSyncGlobals {
  _stgModelRouting?: unknown;
  _stgModelRoutingSavedJson?: string;
  _stgModelRoutingRevision?: number;
  _stgPendingCredentialSecrets?: Record<string, unknown>;
  _loadModelRoutingAuthority?: (preserveDraft?: boolean) => Promise<unknown>;
  _renderModelRoutingTabs?: () => void;
  _invalidateModelCatalogAaScores?: () => void;
  _frameIsOurs?: (frameOwnerId: unknown) => boolean;
  pushSubscribe?: (
    channel: string,
    taskId: string,
    handler: (frame: unknown) => void,
  ) => void;
  pushOnReconnect?: (handler: () => void) => void;
  debugLog?: (message: string, level?: string) => void;
}

function globals(): ModelRoutingSyncGlobals {
  return featureRegistry as unknown as ModelRoutingSyncGlobals;
}

function settingsModalOpen(): boolean {
  return document.getElementById('settingsModal')?.classList.contains('open') === true;
}

/* Never clobber an in-progress edit: the Providers tab holds unsaved
 * credential input and the reasoning-baseline editor stages profile changes;
 * a live reload would drop both. The next Save or settings open reconciles. */
function draftDirty(): boolean {
  const bridge = globals();
  if (!bridge._stgModelRouting) return false;
  if (JSON.stringify(bridge._stgModelRouting) !== bridge._stgModelRoutingSavedJson) {
    return true;
  }
  return Object.keys(bridge._stgPendingCredentialSecrets || {}).length > 0;
}

async function revalidateModelRouting(trigger: string): Promise<void> {
  const bridge = globals();
  const hadDocument = !!bridge._stgModelRouting;
  // Nothing staged and nothing visible: the next settings open fetches fresh.
  if (!hadDocument && !settingsModalOpen()) return;
  if (draftDirty()) return;
  if (typeof bridge._loadModelRoutingAuthority !== 'function') return;
  // preserveDraft guards a document already staged; with none there is no
  // draft to protect (the loader's preserveDraft path no-ops on a null doc).
  const loaded = await bridge._loadModelRoutingAuthority(hadDocument);
  if (!loaded || draftDirty() || !settingsModalOpen()) return;
  bridge._invalidateModelCatalogAaScores?.();
  bridge._renderModelRoutingTabs?.();
  bridge.debugLog?.('[Settings] model-routing hot update applied (' + trigger + ')', 'info');
}

let wired = false;

/** Subscribe once to model-routing wake hints; safe to call repeatedly. */
export function wireModelRoutingLiveSync(): void {
  if (wired) return;
  const bridge = globals();
  if (typeof bridge.pushSubscribe !== 'function') return;
  wired = true;
  bridge.pushSubscribe('notify', '*', (frame: unknown) => {
    const narrowed = narrowModelRoutingChangedFrame(frame);
    if (!narrowed) return;
    const isOurs = globals()._frameIsOurs;
    if (typeof isOurs === 'function' && !isOurs(narrowed.userId)) return;
    // Revision gate: suppress a read the staged authority already reached.
    const stagedRevision = Number(globals()._stgModelRoutingRevision || 0);
    if (Number.isSafeInteger(narrowed.rev) && (narrowed.rev as number) > 0
        && stagedRevision >= (narrowed.rev as number)) {
      return;
    }
    void revalidateModelRouting('push');
  });
  if (typeof bridge.pushOnReconnect === 'function') {
    bridge.pushOnReconnect(() => {
      void revalidateModelRouting('push-reconnect');
    });
  }
}

wireModelRoutingLiveSync();
