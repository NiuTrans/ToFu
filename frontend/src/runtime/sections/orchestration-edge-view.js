/* ===== migrated source: orchestration-edge-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-edge-view.js — SVG connection presentation + selection

   Renders routed edges produced by orchestration-canvas.js and binds pointer
   and keyboard selection without embedding imported edge IDs in executable
   attributes. It owns no topology or selection state.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationEdgeView(options) {
  options = options || {};
  var renderedEdgeIds = [];

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function _select(event, edge) {
    if (event && event.type === 'keydown'
        && event.key !== 'Enter' && event.key !== ' ') return;
    if (event && event.type === 'keydown') event.preventDefault();
    if (typeof options.onSelect === 'function') options.onSelect(edge.id);
  }

  function _issueSummary(edge, index) {
    return typeof options.issueSummary === 'function'
      ? options.issueSummary(edge, index) : null;
  }

  function _edgeLabel(edge, issueLabel) {
    var nodeLabel = typeof options.nodeLabel === 'function'
      ? options.nodeLabel : function (id) { return id; };
    var label = _translate('orch.edge.label', {
      from: nodeLabel(edge.from),
      to: nodeLabel(edge.to),
    });
    if (issueLabel) label += ' · ' + issueLabel;
    return label + ' · ' + _translate('orch.edge.clickTip');
  }

  function render(svg, canvas) {
    if (!svg || !canvas) return;
    var focusedEdgeId = null;
    var active = (options.document || document).activeElement;
    if (active && svg.contains(active)
        && active.classList.contains('orch-edge-path')) {
      var focusedIndex = Number(active.getAttribute('data-edge-index'));
      if (Number.isInteger(focusedIndex)) {
        focusedEdgeId = renderedEdgeIds[focusedIndex] || null;
      }
    }
    var geometry = options.geometry;
    var edges = typeof options.edges === 'function' ? options.edges() : [];
    var selected = typeof options.selectedEdgeId === 'function'
      ? options.selectedEdgeId() : null;
    var portCenter = options.portCenter || function () { return null; };
    var scene = svg.parentElement;
    var modelWidth = scene && scene.getAttribute('data-orch-model-width');
    var modelHeight = scene && scene.getAttribute('data-orch-model-height');
    svg.setAttribute('width', modelWidth || String(canvas.scrollWidth));
    svg.setAttribute('height', modelHeight || String(canvas.scrollHeight));

    var parts = '<defs><marker id="orchArrow" viewBox="0 0 12 12" '
      + 'refX="9.5" refY="6" markerWidth="8" markerHeight="8" '
      + 'orient="auto-start-reverse"><path class="orch-edge-arrow" '
      + 'd="M1 1 L11 6 L1 11 L4 6 Z"></path></marker></defs>';
    var routes = geometry.edgeRoutes(edges, portCenter);
    routes.forEach(function (route, index) {
      var isSelected = selected === route.edge.id;
      var edgeIndex = edges.indexOf(route.edge);
      var issues = _issueSummary(route.edge, edgeIndex);
      var issueClass = issues && issues.total
        ? (issues.errors ? ' has-errors' : ' has-warnings') : '';
      var issueLabel = issues && issues.total
        ? _translate('orch.issues.objectSummary', {
            errors: issues.errors || 0, warnings: issues.warnings || 0,
          }) : '';
      var label = _edgeLabel(route.edge, issueLabel);
      parts += '<path class="orch-edge-hit" d="' + _escape(route.path)
        + '" data-edge-index="' + index + '" aria-hidden="true"></path>';
      parts += '<path class="orch-edge-path' + (isSelected ? ' is-selected' : '')
        + issueClass
        + '" marker-end="url(#orchArrow)" d="' + _escape(route.path)
        + '" data-edge-index="' + index + '" tabindex="0" role="button" '
        + 'aria-pressed="' + isSelected + '" aria-label="'
        + _escape(label) + '"><title>'
        + _escape(label)
        + '</title></path>';
    });

    var connection = typeof options.connection === 'function'
      ? options.connection() : null;
    if (connection) {
      var source = portCenter(connection.from, 'out');
      if (source) {
        parts += '<path class="orch-edge-temp" d="'
          + _escape(geometry.bezier(source, {
            x: connection.x, y: connection.y,
          })) + '"></path>';
      }
    }
    svg.innerHTML = parts;
    renderedEdgeIds = routes.map(function (route) { return route.edge.id; });

    Array.prototype.forEach.call(
      svg.querySelectorAll('[data-edge-index]'), function (path) {
        var route = routes[Number(path.getAttribute('data-edge-index'))];
        if (!route) return;
        path.addEventListener('click', function (event) {
          _select(event, route.edge);
        });
        if (path.classList.contains('orch-edge-path')) {
          path.addEventListener('keydown', function (event) {
            _select(event, route.edge);
          });
        }
      }
    );
    var keyboard = createOrchestrationRovingItemsController({
      root: svg,
      selector: '.orch-edge-path',
    });
    var focusedEdgeIndex = renderedEdgeIds.indexOf(focusedEdgeId);
    var focusedPath = focusedEdgeIndex < 0 ? null
      : svg.querySelector('.orch-edge-path[data-edge-index="'
        + focusedEdgeIndex + '"]');
    keyboard.sync(focusedPath || svg.querySelector('.orch-edge-path.is-selected'));
    if (focusedPath && typeof focusedPath.focus === 'function') {
      focusedPath.focus({ preventScroll: true });
    }
  }

  return { render: render };
}

