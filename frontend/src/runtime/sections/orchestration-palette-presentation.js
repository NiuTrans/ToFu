/* ===== migrated source: orchestration-palette-presentation.js ===== */
/* Pure backend catalogue and safe HTML projection for the node palette. */

function createOrchestrationPalettePresentation(options) {
  options = options || {};
  var escape = options.escape || function (value) { return String(value || ''); };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};
  var glyphs = options.glyphs || {};

  function _roles() {
    return typeof options.roles === 'function' ? options.roles() : [];
  }

  function _controls() {
    return typeof options.controls === 'function' ? options.controls() : [];
  }

  function availability() {
    var state = typeof options.contractState === 'function'
      ? options.contractState() : null;
    if (state && typeof state === 'object') {
      return {
        ready: state.ready === true,
        settled: state.settled === true,
        failed: !!state.error,
      };
    }
    return {
      ready: typeof options.available !== 'function' || !!options.available(),
      settled: typeof options.settled === 'function' && !!options.settled(),
      failed: typeof options.error === 'function' && !!options.error(),
    };
  }

  function _localizedName(prefix, name, fallback) {
    var key = prefix + name;
    var value = translate(key);
    return value && value !== key ? value : fallback;
  }

  function _searchText(parts) {
    return parts.filter(function (value) { return value != null && value !== ''; })
      .join(' ').toLowerCase();
  }

  function chipKey(chip) {
    if (!chip || typeof chip.getAttribute !== 'function') return '';
    return [
      chip.getAttribute('data-ptype') || '',
      chip.getAttribute('data-prole') || '',
      chip.getAttribute('data-pkind') || '',
    ].join('\u0000');
  }

  function _controlHtml(control) {
    var label = _localizedName('orch.controlName.', control.kind, control.label);
    return '<div class="orch-chip orch-chip-ctrl" draggable="true" '
      + 'data-ptype="control" data-pkind="' + escape(control.kind) + '" '
      + 'data-palette-search="' + escape(_searchText([
        control.kind, control.label, label, control.blurb,
      ])) + '" '
      + 'role="button" tabindex="0" aria-label="'
      + escape(translate('orch.palette.add', { name: label })) + '" '
      + 'style="--chip-accent:' + escape(control.accent) + '" title="'
      + escape(control.blurb) + '">'
      + '<span class="orch-chip-glyph">' + (glyphs[control.glyph] || '') + '</span>'
      + '<span class="orch-chip-label">' + escape(label) + '</span></div>';
  }

  function _roleHtml(role) {
    var label = _localizedName('orch.roleName.', role.role, role.label);
    var src = typeof options.iconSrc === 'function' ? options.iconSrc(role.icon) : '';
    return '<div class="orch-chip orch-chip-role" draggable="true" '
      + 'data-ptype="role" data-prole="' + escape(role.role) + '" '
      + 'data-palette-search="' + escape(_searchText([
        role.role, role.label, label, role.blurb,
      ])) + '" '
      + 'role="button" tabindex="0" aria-label="'
      + escape(translate('orch.palette.add', { name: label })) + '" '
      + 'title="' + escape(role.blurb) + '">'
      + '<span class="orch-chip-ava"><img src="' + escape(src) + '" alt="" '
      + 'data-orch-palette-avatar></span>'
      + '<span class="orch-chip-label">' + escape(label) + '</span></div>';
  }

  function _shellHtml() {
    return '<div class="orch-sheet-head orch-m-only"><span>'
      + icons.plus + ' ' + escape(translate('orch.palette.agents')) + '</span>'
      + '<button type="button" class="orch-icon-btn" data-palette-close title="'
      + escape(translate('orch.tip.close')) + '" aria-label="'
      + escape(translate('orch.tip.close')) + '">' + icons.reject + '</button></div>'
      + '<div class="orch-m-only orch-sheet-hint">'
      + escape(translate('orch.palette.tapHint')) + '</div>';
  }

  function loadingHtml(state) {
    var unavailable = state.settled || state.failed;
    return _shellHtml() + '<div class="orch-pal-loading" role="'
      + (unavailable ? 'alert' : 'status') + '">'
      + '<div class="orch-pal-loading-copy">'
      + (unavailable ? ''
        : '<span class="orch-pal-loading-dot" aria-hidden="true"></span>')
      + escape(translate(unavailable
        ? 'orch.palette.unavailable' : 'orch.palette.loading')) + '</div>'
      + (unavailable && typeof options.onRetry === 'function'
        ? '<button type="button" class="orch-btn orch-btn-ghost orch-pal-retry" '
          + 'data-orch-contract-retry>'
          + escape(translate('orch.palette.retry')) + '</button>' : '')
      + '</div>';
  }

  function readyHtml(query) {
    var html = _shellHtml()
      + '<div class="orch-pal-search"><input type="search" '
      + 'data-orch-palette-search autocomplete="off" spellcheck="false" value="'
      + escape(query) + '" placeholder="'
      + escape(translate('orch.palette.search')) + '" aria-label="'
      + escape(translate('orch.palette.search')) + '"></div>'
      + '<div class="orch-pal-section" data-palette-category-label="control">'
      + escape(translate('orch.palette.control'))
      + '</div><div class="orch-pal-grid" data-palette-category="control">';
    _controls().forEach(function (control) { html += _controlHtml(control); });
    html += '</div><div class="orch-pal-section" '
      + 'data-palette-category-label="group">'
      + escape(translate('orch.palette.group'))
      + '</div><div class="orch-pal-grid" data-palette-category="group">'
      + '<div class="orch-chip orch-chip-ctrl orch-chip-group" draggable="true" '
      + 'data-ptype="subflow" data-prole="general" role="button" tabindex="0" '
      + 'data-palette-search="' + escape(_searchText([
        'subflow', 'general', translate('orch.group.chip'),
        translate('orch.group.chipTip'),
      ])) + '" '
      + 'aria-label="' + escape(translate('orch.palette.add', {
        name: translate('orch.group.chip'),
      })) + '" style="--chip-accent:#8b5cf6" title="'
      + escape(translate('orch.group.chipTip')) + '">'
      + '<span class="orch-chip-glyph">' + (glyphs.group || '') + '</span>'
      + '<span class="orch-chip-label">' + escape(translate('orch.group.chip'))
      + '</span></div></div><div class="orch-pal-section" '
      + 'data-palette-category-label="agents">'
      + escape(translate('orch.palette.agents'))
      + '</div><div class="orch-pal-grid" data-palette-category="agents">';
    _roles().forEach(function (role) { html += _roleHtml(role); });
    return html + '</div><div class="orch-pal-empty" data-palette-empty '
      + 'role="status" aria-live="polite" hidden>'
      + escape(translate('orch.palette.noMatches'))
      + '</div><div class="orch-pal-foot">'
      + escape(translate('orch.palette.foot')) + '</div>';
  }

  return {
    availability: availability,
    chipKey: chipKey,
    loadingHtml: loadingHtml,
    readyHtml: readyHtml,
  };
}

