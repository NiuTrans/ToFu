/* ===== migrated source: orchestration-palette.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-palette.js — node-library view + input interactions

   Owns filtering, focus, retry, drag, click, keyboard and mobile-sheet
   activation. Backend catalogue/HTML projection lives in the presentation
   sibling. It emits only an add payload; graph state remains elsewhere.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationPaletteView(options) {
  options = options || {};
  var presentation = options.presentation
    || createOrchestrationPalettePresentation(options);
  var query = '';
  var focusedChipKey = '';

  function _applyFilter(element) {
    var normalized = query.trim().toLowerCase();
    var visibleCount = 0;
    element.querySelectorAll('[data-palette-search]').forEach(function (chip) {
      var text = chip.getAttribute('data-palette-search') || '';
      chip.hidden = !!normalized && text.indexOf(normalized) === -1;
      if (!chip.hidden) visibleCount += 1;
    });
    element.querySelectorAll('[data-palette-category]').forEach(function (grid) {
      var visible = Array.prototype.some.call(
        grid.querySelectorAll('[data-palette-search]'),
        function (chip) { return !chip.hidden; }
      );
      grid.hidden = !visible;
      var name = grid.getAttribute('data-palette-category');
      var heading = element.querySelector(
        '[data-palette-category-label="' + name + '"]');
      if (heading) heading.hidden = !visible;
    });
    var empty = element.querySelector('[data-palette-empty]');
    if (empty) empty.hidden = visibleCount !== 0;
    return visibleCount;
  }

  function _bindSearch(element, restoreFocus, onFilter) {
    var input = element.querySelector('[data-orch-palette-search]');
    if (!input) return;
    function update() {
      query = input.value || '';
      _applyFilter(element);
      if (typeof onFilter === 'function') onFilter();
    }
    input.addEventListener('input', update);
    input.addEventListener('search', update);
    _applyFilter(element);
    if (restoreFocus && typeof input.focus === 'function') input.focus();
  }

  function render(element) {
    if (!element) return;
    var active = element.ownerDocument && element.ownerDocument.activeElement;
    var restoreSearchFocus = !!(active && active.hasAttribute
      && active.hasAttribute('data-orch-palette-search'));
    if (active && element.contains(active) && active.classList
        && active.classList.contains('orch-chip')) {
      focusedChipKey = presentation.chipKey(active);
    }
    var availability = presentation.availability();
    if (!availability.ready) {
      var unavailable = availability.settled || availability.failed;
      element.setAttribute('aria-busy', unavailable ? 'false' : 'true');
      element.innerHTML = presentation.loadingHtml(availability);
      var loadingClose = element.querySelector('[data-palette-close]');
      if (loadingClose && typeof options.closeMobile === 'function') {
        loadingClose.addEventListener('click', options.closeMobile);
      }
      var retry = element.querySelector('[data-orch-contract-retry]');
      if (retry) retry.addEventListener('click', function () {
        retry.disabled = true;
        element.setAttribute('aria-busy', 'true');
        var result;
        try {
          result = options.onRetry();
        } catch (error) {
          result = null;
        }
        function settled() { render(element); }
        if (result && typeof result.then === 'function') {
          result.then(settled, settled);
        } else {
          settled();
        }
      });
      return;
    }
    element.removeAttribute('aria-busy');
    element.innerHTML = presentation.readyHtml(query);
    var keyboard;
    _bindSearch(element, restoreSearchFocus, function () {
      if (keyboard) keyboard.sync();
    });
    keyboard = createOrchestrationRovingItemsController({
      root: element,
      selector: '.orch-chip',
      entry: element.querySelector('[data-orch-palette-search]'),
    });
    var focusedChip = Array.prototype.filter.call(
      element.querySelectorAll('.orch-chip'), function (chip) {
        return !chip.hidden && presentation.chipKey(chip) === focusedChipKey;
      }
    )[0] || null;
    if (!restoreSearchFocus && focusedChip) {
      keyboard.sync(focusedChip);
      focusedChip.focus({ preventScroll: true });
    }

    var close = element.querySelector('[data-palette-close]');
    if (close && typeof options.closeMobile === 'function') {
      close.addEventListener('click', options.closeMobile);
    }
    element.querySelectorAll('[data-orch-palette-avatar]').forEach(function (image) {
      image.addEventListener('error', function () {
        image.style.display = 'none';
      }, { once: true });
    });
    element.querySelectorAll('.orch-chip').forEach(function (chip) {
      var dragged = false;
      function payload() {
        return {
          ptype: chip.getAttribute('data-ptype'),
          role: chip.getAttribute('data-prole') || '',
          kind: chip.getAttribute('data-pkind') || '',
        };
      }
      function add() {
        if (typeof options.onAdd === 'function') options.onAdd(payload());
        if (typeof options.isMobile === 'function' && options.isMobile()
            && typeof options.closeMobile === 'function') {
          options.closeMobile();
        }
      }
      chip.addEventListener('dragstart', function (event) {
        dragged = true;
        if (event.dataTransfer) {
          event.dataTransfer.setData('text/orch', JSON.stringify(payload()));
          event.dataTransfer.effectAllowed = 'copy';
        }
      });
      chip.addEventListener('dragend', function () {
        setTimeout(function () { dragged = false; }, 0);
      });
      chip.addEventListener('click', function () {
        if (!dragged) add();
      });
      chip.addEventListener('keydown', function (event) {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        add();
      });
    });
  }

  return {
    query: function () { return query; },
    render: render,
  };
}

