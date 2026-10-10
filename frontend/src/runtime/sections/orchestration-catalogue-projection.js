/* ===== migrated source: orchestration-catalogue-projection.js ===== */
/* Merge backend authoring policy into the frontend presentation catalogue. */

function createOrchestrationCatalogueProjection(options) {
  options = options || {};

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function title(name) {
    return String(name || '').replace(/_/g, ' ')
      .replace(/\b\w/g, function (character) {
        return character.toUpperCase();
      });
  }

  var roles = clone(options.roles || []);
  var controls = clone(options.controls || []);

  function adoptRoles(names, personas, nodeDefaults) {
    if (!Array.isArray(names) || !names.length) return;
    personas = personas && typeof personas === 'object' ? personas : {};
    nodeDefaults = nodeDefaults && typeof nodeDefaults === 'object'
      ? nodeDefaults : {};
    var roleDefaults = nodeDefaults.roles || {};
    var byRole = {};
    roles.forEach(function (role) { byRole[role.role] = role; });
    roles = names.map(function (name) {
      var persona = personas[name] || {};
      var role = clone(byRole[name] || {
        role: name,
        label: title(name),
        icon: 'tofu-general',
        blurb: '',
      });
      var defaults = roleDefaults[name] || nodeDefaults.genericRole || {};
      if (defaults.tier) role.tier = defaults.tier;
      if (persona.tier) role.tier = persona.tier;
      if (persona.whenToUse) role.blurb = persona.whenToUse;
      if (!role.blurb && typeof options.translate === 'function') {
        role.blurb = options.translate('orch.role.genericBlurb');
      }
      return role;
    });
  }

  function adoptControls(contractControls) {
    if (!contractControls || typeof contractControls !== 'object') return;
    var byKind = {};
    controls.forEach(function (control) { byKind[control.kind] = control; });
    var ordered = controls.map(function (control) { return control.kind; })
      .filter(function (kind) {
        return Object.prototype.hasOwnProperty.call(contractControls, kind);
      });
    Object.keys(contractControls).forEach(function (kind) {
      if (ordered.indexOf(kind) === -1) ordered.push(kind);
    });
    controls = ordered.map(function (kind) {
      var control = clone(byKind[kind] || {
        kind: kind,
        label: title(kind),
        glyph: 'branch',
        accent: '#64748b',
        blurb: typeof options.translate === 'function'
          ? options.translate('orch.control.genericBlurb') : '',
      });
      control.single = !!(contractControls[kind] || {}).single;
      return control;
    });
  }

  function snapshot() {
    return {
      roles: clone(roles),
      controls: clone(controls),
    };
  }

  function adopt(source) {
    source = source && typeof source === 'object' ? source : {};
    adoptRoles(source.roleNames, source.personas, source.nodeDefaults);
    adoptControls(source.controls);
    return snapshot();
  }

  return Object.freeze({
    adopt: adopt,
    snapshot: snapshot,
  });
}

