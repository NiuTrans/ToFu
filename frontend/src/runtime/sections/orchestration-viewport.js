/* ===== migrated source: orchestration-viewport.js ===== */
/* ════════════════════════════════════════════════════════════════════
   orchestration-viewport.js — Canvas zoom, extent and fit-to-view

   Owns presentation-only viewport state. Authoring coordinates remain in
   graph/model units, so zoom never dirties the document or enters history.
   Geometry consumers read transform() through one injected seam.
   ════════════════════════════════════════════════════════════════════ */


function createOrchestrationViewportController(options) {
  options = options || {};
  var doc = options.document || document;
  var scale = Number(options.defaultScale) || 1;
  var offsetX = 0;
  var offsetY = 0;
  var wired = false;
  var geometry = options.geometry || createOrchestrationViewportGeometry({
    minScale: options.minScale,
    maxScale: options.maxScale,
    cardWidth: options.cardWidth,
    cardHeight: options.cardHeight,
    padding: options.padding,
  });
  var minScale = geometry.minScale();
  var maxScale = geometry.maxScale();
  var step = Number(options.step) || 0.1;
  var padding = geometry.padding();

  function fitScaleFloor() {
    var configured = typeof options.fitMinScale === 'function'
      ? options.fitMinScale() : options.fitMinScale;
    var value = Number(configured);
    return Number.isFinite(value) && value > 0
      ? geometry.clampScale(value) : minScale;
  }

  function canvas() { return doc.getElementById('orchCanvas'); }
  function extent() { return doc.getElementById('orchViewportExtent'); }
  function scene() { return doc.getElementById('orchViewportScene'); }
  function edges() { return doc.getElementById('orchEdges'); }
  function nodesElement() { return doc.getElementById('orchNodes'); }
  function nodes() {
    var value = typeof options.nodes === 'function' ? options.nodes() : [];
    return Array.isArray(value) ? value : [];
  }
  function clamp(value) {
    return geometry.clampScale(value);
  }

  function transform() {
    return { scale: scale, offsetX: offsetX, offsetY: offsetY };
  }

  function bounds() {
    return geometry.bounds(nodes(), function (node) {
      var element = doc.getElementById('orch-node-' + node.id);
      return element && Number(element.offsetHeight) || 0;
    });
  }

  function renderControls() {
    var label = doc.getElementById('orchZoomResetBtn');
    var zoomOut = doc.getElementById('orchZoomOutBtn');
    var zoomIn = doc.getElementById('orchZoomInBtn');
    if (label) label.textContent = Math.round(scale * 100) + '%';
    if (zoomOut) zoomOut.disabled = scale <= minScale + 0.001;
    if (zoomIn) zoomIn.disabled = scale >= maxScale - 0.001;
  }

  function sync() {
    var viewport = canvas();
    var box = extent();
    var content = scene();
    if (!viewport || !box || !content) return null;
    var graphBounds = bounds();
    var projected = geometry.extent({
      width: viewport.clientWidth, height: viewport.clientHeight,
    }, transform(), graphBounds);
    var modelWidth = projected.modelWidth;
    var modelHeight = projected.modelHeight;
    content.style.width = Math.ceil(modelWidth) + 'px';
    content.style.height = Math.ceil(modelHeight) + 'px';
    content.style.transform = 'translate(' + offsetX + 'px,' + offsetY
      + 'px) scale(' + scale + ')';
    content.setAttribute('data-orch-model-width', String(Math.ceil(modelWidth)));
    content.setAttribute('data-orch-model-height', String(Math.ceil(modelHeight)));
    box.style.width = Math.ceil(projected.visualWidth) + 'px';
    box.style.height = Math.ceil(projected.visualHeight) + 'px';
    var svg = edges();
    if (svg) {
      svg.setAttribute('width', String(Math.ceil(modelWidth)));
      svg.setAttribute('height', String(Math.ceil(modelHeight)));
    }
    var nodeLayer = nodesElement();
    if (nodeLayer) {
      nodeLayer.style.width = Math.ceil(modelWidth) + 'px';
      nodeLayer.style.height = Math.ceil(modelHeight) + 'px';
    }
    viewport.style.backgroundSize = (24 * scale) + 'px ' + (24 * scale) + 'px';
    viewport.style.backgroundPosition = offsetX + 'px ' + offsetY + 'px';
    renderControls();
    return {
      width: modelWidth, height: modelHeight, bounds: graphBounds,
      scale: scale, offsetX: offsetX, offsetY: offsetY,
    };
  }

  function setScale(value, anchor) {
    var viewport = canvas();
    if (!viewport) return false;
    var next = clamp(Number(value) || scale);
    if (Math.abs(next - scale) < 0.001) return false;
    var anchorX = anchor && Number.isFinite(anchor.x)
      ? anchor.x : viewport.clientWidth / 2;
    var anchorY = anchor && Number.isFinite(anchor.y)
      ? anchor.y : viewport.clientHeight / 2;
    var modelX = (viewport.scrollLeft + anchorX - offsetX) / scale;
    var modelY = (viewport.scrollTop + anchorY - offsetY) / scale;
    scale = next;
    sync();
    viewport.scrollLeft = Math.max(0, modelX * scale + offsetX - anchorX);
    viewport.scrollTop = Math.max(0, modelY * scale + offsetY - anchorY);
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function zoomBy(delta, anchor) {
    return setScale(scale + delta, anchor);
  }

  function reset() {
    var viewport = canvas();
    if (!viewport) return false;
    var modelX = (viewport.scrollLeft + viewport.clientWidth / 2 - offsetX) / scale;
    var modelY = (viewport.scrollTop + viewport.clientHeight / 2 - offsetY) / scale;
    scale = 1;
    offsetX = 0;
    offsetY = 0;
    sync();
    viewport.scrollLeft = Math.max(0, modelX - viewport.clientWidth / 2);
    viewport.scrollTop = Math.max(0, modelY - viewport.clientHeight / 2);
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function fit() {
    var viewport = canvas();
    if (!viewport) return false;
    var graphBounds = bounds();
    if (!graphBounds) {
      scale = 1;
      offsetX = 0;
      offsetY = 0;
      sync();
      viewport.scrollLeft = 0;
      viewport.scrollTop = 0;
      return true;
    }
    var fitted = geometry.fit(graphBounds, {
      width: viewport.clientWidth, height: viewport.clientHeight,
    }, fitScaleFloor());
    scale = fitted.scale;
    offsetX = fitted.offsetX;
    offsetY = fitted.offsetY;
    sync();
    viewport.scrollLeft = fitted.scrollLeft;
    viewport.scrollTop = fitted.scrollTop;
    if (typeof options.onChange === 'function') options.onChange(transform());
    return true;
  }

  function wire() {
    var viewport = canvas();
    if (!viewport || wired) return false;
    wired = true;
    viewport.addEventListener('wheel', function (event) {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      var rect = viewport.getBoundingClientRect();
      zoomBy(event.deltaY < 0 ? step : -step, {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    }, { passive: false });
    sync();
    return true;
  }

  return {
    wire: wire,
    sync: sync,
    fit: fit,
    reset: reset,
    zoomIn: function () { return zoomBy(step); },
    zoomOut: function () { return zoomBy(-step); },
    setScale: setScale,
    scale: function () { return scale; },
    transform: transform,
    bounds: bounds,
  };
}

