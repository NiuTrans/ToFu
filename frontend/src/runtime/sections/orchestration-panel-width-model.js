/* ===== migrated source: orchestration-panel-width-model.js ===== */
/* Pure responsive width/preference model for Studio desktop rails. */

function createOrchestrationPanelWidthModel(options) {
  options = options || {};
  var specs = options.specs || {};
  var minCanvasWidth = Number(options.minCanvasWidth) || 360;
  var handleSpace = Number(options.handleSpace) || 0;
  var customized = {};
  var preferred = {};
  var widths = {};

  function names() { return Object.keys(specs); }
  function valid(name) {
    return Object.prototype.hasOwnProperty.call(specs, name);
  }
  function compact() {
    return typeof options.compact === 'function' && !!options.compact();
  }
  function expanded(name) {
    if (typeof options.isExpanded !== 'function') return true;
    try { return options.isExpanded(name) !== false; }
    catch (_error) { return true; }
  }
  function defaultWidth(name) {
    var spec = specs[name];
    return compact() ? spec.compact : spec.normal;
  }
  function bounds(name) {
    var spec = specs[name];
    if (!spec) return { min: 0, max: 0 };
    var other = names().find(function (candidate) {
      return candidate !== name;
    });
    var available = typeof options.surfaceWidth === 'function'
      ? Math.max(0, Number(options.surfaceWidth()) || 0) : 0;
    var otherWidth = other && expanded(other) ? Number(widths[other]) || 0 : 0;
    var dynamicMax = available
      ? available - otherWidth - minCanvasWidth - handleSpace
      : spec.max;
    return Object.freeze({
      min: spec.min,
      max: Math.max(spec.min, Math.min(spec.max, dynamicMax)),
    });
  }
  function clamp(name, value) {
    var range = bounds(name);
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) numeric = defaultWidth(name);
    return Math.round(Math.max(range.min, Math.min(range.max, numeric)));
  }
  function snapshot() {
    var value = {};
    names().forEach(function (name) { value[name] = widths[name] || 0; });
    return Object.freeze(value);
  }
  function persisted() {
    var value = {};
    names().forEach(function (name) {
      if (customized[name]) value[name] = preferred[name];
    });
    return Object.freeze(value);
  }
  function hydrate(stored) {
    stored = stored && typeof stored === 'object' ? stored : {};
    names().forEach(function (name) {
      var own = Object.prototype.hasOwnProperty.call(stored, name);
      var value = own ? Number(stored[name]) : NaN;
      customized[name] = Number.isFinite(value) && value > 0;
      preferred[name] = customized[name]
        ? Math.round(Math.max(specs[name].min, Math.min(specs[name].max, value)))
        : defaultWidth(name);
      widths[name] = preferred[name];
    });
    return sync();
  }
  function setWidth(name, value) {
    if (!valid(name)) return 0;
    customized[name] = true;
    widths[name] = clamp(name, value);
    preferred[name] = widths[name];
    return widths[name];
  }
  function reset(name) {
    if (!valid(name)) return 0;
    customized[name] = false;
    preferred[name] = defaultWidth(name);
    widths[name] = clamp(name, preferred[name]);
    return widths[name];
  }
  function sync() {
    names().forEach(function (name) {
      if (!customized[name]) preferred[name] = defaultWidth(name);
      widths[name] = clamp(name, preferred[name]);
    });
    return snapshot();
  }

  return Object.freeze({
    bounds: bounds,
    hydrate: hydrate,
    persisted: persisted,
    reset: reset,
    setWidth: setWidth,
    snapshot: snapshot,
    sync: sync,
  });
}

