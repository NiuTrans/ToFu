/* ===== migrated source: orchestration-export.js ===== */
/* Definition export boundary for Orchestration Studio.
 *
 * Owns JSON download naming, browser object-URL cleanup and user feedback.
 * Callers provide only a root snapshot; write-conflict recovery and toolbar
 * export therefore cannot assemble different download behavior.
 */

function createOrchestrationExportController(options) {
  options = options || {};
  var doc = options.document
    || (typeof document !== 'undefined' ? document : null);
  var Url = options.urlApi
    || (typeof URL !== 'undefined' ? URL : null);
  var BlobType = options.Blob
    || (typeof Blob !== 'undefined' ? Blob : null);
  var schedule = options.schedule
    || (typeof setTimeout === 'function' ? setTimeout : null);

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function notify(key, params, isError) {
    if (typeof options.toast !== 'function') return;
    try {
      options.toast(translate(key, params), !!isError);
    } catch (error) {
      report('notify', error);
    }
  }

  function report(context, error) {
    return reportOrchestrationDiagnostic(options.onError, context, error);
  }

  function filenameFor(definition) {
    var raw = definition && definition.name ? definition.name : 'flow';
    var stem = String(raw).trim()
      .replace(/[^a-z0-9_-]+/gi, '_')
      .replace(/^_+|_+$/g, '')
      .toLowerCase();
    return (stem || 'flow') + '.orch.json';
  }

  function revoke(url) {
    if (!url || !Url || typeof Url.revokeObjectURL !== 'function') return;
    try { Url.revokeObjectURL(url); } catch (error) { report('revoke', error); }
  }

  function deferRevoke(url) {
    if (typeof schedule !== 'function') {
      revoke(url);
      return;
    }
    try {
      schedule(function () { revoke(url); }, 1000);
    } catch (error) {
      report('schedule-revoke', error);
      revoke(url);
    }
  }

  function exportDefinition(definition) {
    var anchor = null;
    var url = null;
    try {
      if (!definition || typeof definition !== 'object') {
        throw new Error('A definition object is required');
      }
      if (!doc || !doc.body || typeof doc.createElement !== 'function'
          || !BlobType || !Url || typeof Url.createObjectURL !== 'function') {
        throw new Error('Browser download APIs are unavailable');
      }
      var filename = filenameFor(definition);
      var payload = JSON.stringify(definition, null, 2);
      var blob = new BlobType([payload], { type: 'application/json' });
      url = Url.createObjectURL(blob);
      anchor = doc.createElement('a');
      anchor.href = url;
      anchor.download = filename;
      doc.body.appendChild(anchor);
      anchor.click();
      deferRevoke(url);
      url = null;
      notify('orch.export.done', { file: filename }, false);
      return filename;
    } catch (error) {
      revoke(url);
      report('export', error);
      notify('orch.export.failed', null, true);
      return null;
    } finally {
      if (anchor && typeof anchor.remove === 'function') anchor.remove();
    }
  }

  function exportCurrent() {
    var definition = typeof options.snapshot === 'function'
      ? options.snapshot() : null;
    return exportDefinition(definition);
  }

  return {
    exportCurrent: exportCurrent,
    exportDefinition: exportDefinition,
    filenameFor: filenameFor,
  };
}

