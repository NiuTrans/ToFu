/* ===== migrated source: core/model_fold.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   core/model_fold.js — THE display-fold rule for model pickers (SSOT)

   WHY THIS EXISTS
   ---------------
   A gateway endpoint (Meituan et al.) can expose dozens of rows that are
   really ONE thing: cloud mirrors of a single deployment, or a long version
   series of one line (glm-5.1 / 5.2 / 5.3). The server already KNOWS these
   relations (lib/model_info/_folds.py) and ships them on every model entry
   as fold_group / fold_canonical / family / family_primary. This module is
   the ONE place that turns a flat, display-sorted model list into RENDER
   UNITS, so the toolbar picker (_populateModelDropdown) and the Settings
   visibility lists (_renderDropdownVisibility / _renderIgVisibility) can
   never disagree about what folds into what — the same contract
   core/model_group.js established for grouping.

   UNIT CONTRACT
   -------------
     { kind: 'single'|'alias'|'family',
       face: <entry>,            // the row's identity
       members: [<entry> …],     // every entry the unit stands for
       children: [<unit> …] }    // family only: member units (single/alias)

   - alias unit: members are interchangeable wire spellings; face = the
     fold_canonical member.
   - family unit: children are the version units; face = the
     family_primary member. If the primary is filtered out (user-hidden),
     the first visible unit fronts the family — a fold DEGRADES, it never
     strands a row.

   Pure module: no DOM, no network. localStorage access (recent models) is
   wrapped — private-mode browsers throw on write, and a picker must never
   die because a recents list could not persist.
   ═══════════════════════════════════════════════════════════════════ */

(function() {
  /**
   * Fold a display-sorted model list into render units.
   * @param {Array} models  entries in final display order, optionally
   *   carrying server fold metadata (fold_group / fold_canonical / family /
   *   family_primary). Entries WITHOUT metadata pass through as singles, so
   *   a stale payload (or a stale backend) renders exactly the old flat list.
   * @returns {Array} units in display order (family units sit at their
   *   face's sorted position).
   */
  function modelDisplayUnits(models) {
    models = models || [];
    var i, k, m;

    /* Pass 1 — ALIAS fold: entries sharing a fold_group collapse into one
     * unit faced by the fold_canonical member. A group that arrived with a
     * single visible member renders as a plain single (folding a solo row
     * under itself would just add a badge that hides nothing). */
    var byFold = {};
    for (i = 0; i < models.length; i++) {
      m = models[i];
      if (m && m.fold_group) {
        (byFold[m.fold_group] = byFold[m.fold_group] || []).push(m);
      }
    }
    var foldClaimed = {};
    var units = [];
    for (i = 0; i < models.length; i++) {
      m = models[i];
      if (m && m.fold_group && (byFold[m.fold_group] || []).length > 1) {
        if (foldClaimed[m.fold_group]) continue;
        foldClaimed[m.fold_group] = true;
        var g = byFold[m.fold_group];
        var face = g[0];
        for (k = 0; k < g.length; k++) {
          if (g[k].model_id === m.fold_canonical) { face = g[k]; break; }
        }
        units.push({ kind: 'alias', face: face, members: g });
      } else {
        units.push({ kind: 'single', face: m, members: [m] });
      }
    }

    /* Pass 2 — FAMILY fold over units: units whose face carries a shared
     * family key fold under the unit fronted by family_primary. Members
     * stay units (an alias unit inside a family keeps its own badge), so
     * mirrors remain reachable one level down instead of vanishing. */
    var byFam = {};
    for (i = 0; i < units.length; i++) {
      var f = units[i].face;
      if (f && f.family) {
        (byFam[f.family] = byFam[f.family] || []).push(units[i]);
      }
    }
    var famClaimed = {};
    var out = [];
    for (i = 0; i < units.length; i++) {
      var u = units[i];
      f = u.face;
      if (f && f.family && (byFam[f.family] || []).length > 1) {
        if (famClaimed[f.family]) continue;
        famClaimed[f.family] = true;
        var children = byFam[f.family];
        var faceUnit = children[0];
        for (k = 0; k < children.length; k++) {
          if (children[k].face && children[k].face.model_id === f.family_primary) {
            faceUnit = children[k];
            break;
          }
        }
        var flat = [];
        for (k = 0; k < children.length; k++) {
          flat = flat.concat(children[k].members);
        }
        out.push({ kind: 'family', face: faceUnit.face,
                   members: flat, children: children });
      } else {
        out.push(u);
      }
    }
    return out;
  }

  /* ── Recent models (picker pin section) ──
   * Key follows the tofu_* localStorage namespacing (tofu_ui_lang et al.). */
  var RECENT_KEY = 'tofu_recent_models';
  var RECENT_MAX = 5;

  /** @returns {string[]} most-recent-first model ids (never throws). */
  function recentModels() {
    try {
      var raw = localStorage.getItem(RECENT_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(arr)) return [];
      return arr.filter(function(x) { return typeof x === 'string' && x; })
                .slice(0, RECENT_MAX);
    } catch (e) {
      return [];
    }
  }

  /** Record a selection at the front of the recents list (never throws). */
  function pushRecentModel(modelId) {
    if (!modelId) return;
    try {
      var arr = recentModels().filter(function(x) { return x !== modelId; });
      arr.unshift(modelId);
      localStorage.setItem(RECENT_KEY, JSON.stringify(arr.slice(0, RECENT_MAX)));
    } catch (e) {
      /* storage unavailable (private mode / quota) — recents simply don't
       * persist this session; the picker works unchanged. */
    }
  }

  runtimeScope.modelDisplayUnits = modelDisplayUnits;
  runtimeScope.recentModels = recentModels;
  runtimeScope.pushRecentModel = pushRecentModel;
  /* Publish on the real global object too — the concatenated sections'
   * cross-module contract is the GLOBAL scope; see the note in
   * core/model_group.js for the full rationale (the 2026-08-14 oauth-group
   * divergence). Keep until every consumer reads runtimeScope. */
  if (typeof globalThis !== 'undefined') {
    globalThis.modelDisplayUnits = modelDisplayUnits;
    globalThis.recentModels = recentModels;
    globalThis.pushRecentModel = pushRecentModel;
  }
})();
