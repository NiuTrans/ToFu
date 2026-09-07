/* ===== migrated source: orchestration-studio-services.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-studio-services.js — shared Studio application services

   Owns the stable browser/runtime dependencies injected into the Studio
   controller graph. API discovery stays late-bound; presentation services
   retain one identity and error reporting follows one scoped convention.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationStudioServices(options) {
  options = options || {};
  var doc = options.document ||
    (typeof document !== 'undefined' ? document : null);
  var win = options.window ||
    (typeof window !== 'undefined' ? window : null);
  var reporters = Object.create(null);

  function api() {
    if (typeof options.api === 'function') return options.api();
    if (options.api != null) return options.api;
    return typeof runtimeScope.resolveOrchestrationApiClient === 'function'
      ? runtimeScope.resolveOrchestrationApiClient() : null;
  }

  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function richCopy(value) {
    return typeof options.richCopy === 'function'
      ? options.richCopy(value) : escape(value);
  }

  function toast() {
    if (typeof options.toast === 'function') {
      return options.toast.apply(null, arguments);
    }
  }

  function warn() {
    if (typeof options.warn === 'function') {
      return options.warn.apply(null, arguments);
    }
  }

  function choose(config) {
    return typeof options.choose === 'function'
      ? options.choose(config) : Promise.resolve('keep');
  }

  function confirm(message, config, fallback) {
    return typeof options.confirm === 'function'
      ? options.confirm(message, config, fallback)
      : Promise.resolve(fallback === undefined ? true : fallback);
  }

  function reportError(scope, context, error) {
    if (typeof options.reportError === 'function') {
      return reportOrchestrationDiagnostic(
        options.reportError, scope, context, error);
    }
    var logger = options.logger;
    if (logger && typeof logger.warn === 'function') {
      return reportOrchestrationDiagnostic(
        logger.warn.bind(logger),
        '[' + scope + '] ' + context + ' failed:', error);
    }
    return false;
  }

  function reporter(scope, defaultContext) {
    var key = String(scope || 'Orchestration') + '\n'
      + String(defaultContext || 'operation');
    if (!reporters[key]) {
      reporters[key] = function (context, error) {
        if (arguments.length < 2) {
          error = context;
          context = defaultContext || 'operation';
        }
        return reportError(scope || 'Orchestration', context, error);
      };
    }
    return reporters[key];
  }

  return Object.freeze({
    document: doc,
    window: win,
    api: api,
    translate: translate,
    escape: escape,
    richCopy: richCopy,
    toast: toast,
    warn: warn,
    choose: choose,
    confirm: confirm,
    reportError: reportError,
    reporter: reporter,
  });
}

// Production environment adapter. Every global is resolved at call time where
// replacement is meaningful (API, dialogs, feedback, translation), so this
// module can load before the controller graph without capturing stale state.
var _orchServices = createOrchestrationStudioServices({
  document: typeof document !== 'undefined' ? document : null,
  window: typeof window !== 'undefined' ? window : null,
  translate: function (key, params) {
    return typeof t === 'function' ? t(key, params) : key;
  },
  escape: function (value) {
    return typeof escapeHtml === 'function'
      ? escapeHtml(value) : String(value == null ? '' : value);
  },
  richCopy: function (value) {
    if (typeof formatOrchestrationRichCopy === 'function') {
      return formatOrchestrationRichCopy(value);
    }
    return typeof escapeHtml === 'function'
      ? escapeHtml(value) : String(value == null ? '' : value);
  },
  toast: function () {
    return typeof _orchFeedback !== 'undefined' && _orchFeedback
      ? _orchFeedback.toast.apply(null, arguments) : null;
  },
  warn: function () {
    return typeof _orchFeedback !== 'undefined' && _orchFeedback
      ? _orchFeedback.warn.apply(null, arguments) : null;
  },
  choose: function (config) {
    return typeof showChoice === 'function'
      ? showChoice(config) : Promise.resolve('keep');
  },
  confirm: function (message, options, fallback) {
    return typeof showConfirm === 'function'
      ? showConfirm(message, options)
      : Promise.resolve(fallback === undefined ? true : fallback);
  },
  logger: typeof console !== 'undefined' ? console : null,
});

