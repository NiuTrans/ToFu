/* ===== migrated source: orchestration-viewport-geometry.js ===== */
/* Pure Canvas viewport geometry.
 *
 * Graph/model coordinates enter here; DOM measurements and style writes stay
 * in orchestration-viewport.js. Keeping the calculations free of browser
 * state makes fit/extent policy reusable and independently testable.
 */

function createOrchestrationViewportGeometry(options) {
  options = options || {};
  var minimum = Number(options.minScale) || 0.35;
  var maximum = Number(options.maxScale) || 1.5;
  var cardWidth = Number(options.cardWidth) || 188;
  var cardHeight = Number(options.cardHeight) || 76;
  var padding = Number(options.padding) || 44;

  function clampScale(value) {
    var numeric = Number(value);
    if (!Number.isFinite(numeric)) numeric = 1;
    return Math.min(maximum, Math.max(minimum, numeric));
  }

  function graphBounds(nodes, measureHeight) {
    var list = Array.isArray(nodes) ? nodes : [];
    if (!list.length) return null;
    var minX = Infinity;
    var minY = Infinity;
    var maxX = -Infinity;
    var maxY = -Infinity;
    list.forEach(function (node) {
      var x = Number(node.x) || 0;
      var y = Number(node.y) || 0;
      var measured = typeof measureHeight === 'function'
        ? Number(measureHeight(node)) : 0;
      var height = measured > 0 ? measured : cardHeight;
      minX = Math.min(minX, x);
      minY = Math.min(minY, y);
      maxX = Math.max(maxX, x + cardWidth);
      maxY = Math.max(maxY, y + height);
    });
    return Object.freeze({
      minX: minX, minY: minY, maxX: maxX, maxY: maxY,
    });
  }

  function extent(viewport, transform, bounds) {
    viewport = viewport || {};
    transform = transform || {};
    var width = Math.max(0, Number(viewport.width) || 0);
    var height = Math.max(0, Number(viewport.height) || 0);
    var scale = clampScale(transform.scale);
    var offsetX = Number(transform.offsetX) || 0;
    var offsetY = Number(transform.offsetY) || 0;
    var maxX = bounds ? bounds.maxX + padding : 0;
    var maxY = bounds ? bounds.maxY + padding : 0;
    var modelWidth = Math.max(maxX, Math.max(1, (width - offsetX) / scale));
    var modelHeight = Math.max(maxY, Math.max(1, (height - offsetY) / scale));
    return Object.freeze({
      modelWidth: modelWidth,
      modelHeight: modelHeight,
      visualWidth: Math.max(width, offsetX + modelWidth * scale),
      visualHeight: Math.max(height, offsetY + modelHeight * scale),
      scale: scale,
      offsetX: offsetX,
      offsetY: offsetY,
    });
  }

  function fit(bounds, viewport, fitMinScale) {
    if (!bounds) return null;
    viewport = viewport || {};
    var viewportWidth = Math.max(0, Number(viewport.width) || 0);
    var viewportHeight = Math.max(0, Number(viewport.height) || 0);
    var width = Math.max(1, bounds.maxX - bounds.minX);
    var height = Math.max(1, bounds.maxY - bounds.minY);
    var candidate = Math.min(
      1,
      Math.max(1, viewportWidth - padding * 2) / width,
      Math.max(1, viewportHeight - padding * 2) / height
    );
    var scale = Math.max(clampScale(fitMinScale), clampScale(candidate));
    var visualWidth = width * scale;
    var visualHeight = height * scale;
    var offsetX = Math.max(
      padding, (viewportWidth - visualWidth) / 2 - bounds.minX * scale);
    var offsetY = Math.max(
      padding, (viewportHeight - visualHeight) / 2 - bounds.minY * scale);
    var horizontalOverflow = visualWidth + padding * 2 > viewportWidth;
    var verticalOverflow = visualHeight + padding * 2 > viewportHeight;
    return Object.freeze({
      scale: scale,
      offsetX: offsetX,
      offsetY: offsetY,
      scrollLeft: Math.max(0, horizontalOverflow
        ? bounds.minX * scale + offsetX - padding
        : (bounds.minX + bounds.maxX) / 2 * scale + offsetX
          - viewportWidth / 2),
      scrollTop: Math.max(0, verticalOverflow
        ? bounds.minY * scale + offsetY - padding
        : (bounds.minY + bounds.maxY) / 2 * scale + offsetY
          - viewportHeight / 2),
    });
  }

  return Object.freeze({
    bounds: graphBounds,
    clampScale: clampScale,
    extent: extent,
    fit: fit,
    maxScale: function () { return maximum; },
    minScale: function () { return minimum; },
    padding: function () { return padding; },
  });
}

