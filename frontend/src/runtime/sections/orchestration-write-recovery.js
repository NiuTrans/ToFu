/* ===== migrated source: orchestration-write-recovery.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-write-recovery.js — stale-definition recovery flow

   A save conflict is not resolved by a toast. This controller owns the one
   user choice between keeping the local draft, exporting it before loading
   the server version, or deliberately discarding it. Document lifecycle,
   storage transport and file export remain injected ports.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationWriteRecoveryController(options) {
  options = options || {};
  var flights = createOrchestrationSingleFlight();

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function currentId() {
    return typeof options.currentId === 'function'
      ? options.currentId() : null;
  }

  function stillCurrent(conflict, id) {
    if (currentId() !== id) return false;
    return typeof options.isCurrent !== 'function'
      || options.isCurrent(conflict);
  }

  async function run(conflict) {
    conflict = conflict && typeof conflict === 'object' ? conflict : {};
    var id = currentId();
    if (!id || (conflict.operation && conflict.operation !== 'replace')) {
      return { action: 'keep', recovered: false };
    }
    if (typeof options.choose !== 'function') {
      return { action: 'keep', recovered: false };
    }

    var action = await options.choose({
      title: translate('orch.conflict.title'),
      message: translate('orch.conflict.message'),
      dismissValue: 'keep',
      liveCheck: function () { return stillCurrent(conflict, id); },
      options: [
        {
          value: 'export_reload',
          label: translate('orch.conflict.exportReload'),
          subtitle: translate('orch.conflict.exportReloadHint'),
          accent: true,
        },
        {
          value: 'reload',
          label: translate('orch.conflict.reload'),
          subtitle: translate('orch.conflict.reloadHint'),
        },
        {
          value: 'keep',
          label: translate('orch.conflict.keep'),
          subtitle: translate('orch.conflict.keepHint'),
        },
      ],
    });

    if (action !== 'reload' && action !== 'export_reload') {
      return { action: 'keep', recovered: false };
    }
    if (!stillCurrent(conflict, id)) {
      return { action: action, recovered: false, stale: true };
    }
    if (action === 'export_reload') {
      if (typeof options.exportDraft !== 'function') {
        return { action: action, recovered: false };
      }
      var exported = await options.exportDraft();
      if (!exported) return { action: action, recovered: false };
    }
    if (typeof options.loadLatest !== 'function') {
      return { action: action, recovered: false };
    }
    var loaded = await options.loadLatest(id);
    return { action: action, recovered: !!loaded, loaded: loaded || null };
  }

  function open(conflict) {
    return flights.share('recovery', function () { return run(conflict); });
  }

  return {
    open: open,
    pending: function () { return flights.pending('recovery'); },
  };
}

