#!/usr/bin/env python3
"""tests/test_frontend_matrix_wire_pool.py — the matrix renders & probes the
WIRE-ID POOL, not the logical model_id.

WHY
---
Under the model-identity contract (lib/llm_dispatch/model_entry.py)
``model_id`` is a LOGICAL/preset id. When an entry declares ``request_ids``,
the dispatcher sends exactly those ids on the wire — the logical id itself is
never sent. The access matrix used to render/probe ``[model_id] + aliases``
anyway: every cell of an explicit-pool entry tested a (key × id) pair that
can never occur in production, and its verdict (usually a 404 for a name the
gateway doesn't route) fed a false recommend-disable — while the ids that DO
carry real traffic were never probed at all.

WHAT IS PINNED (drives the REAL shipped access_matrix.js under node)
-------------------------------------------------------------------
  * ROWS — an explicit-pool entry renders a LOGICAL header row (global
    toggle + preset badge + pool count, NO per-key cells, NO row probe bolt)
    plus one wire row per pool id (union across the entry and its
    ``key_access`` cells); a legacy entry (``[model_id] + aliases``) renders
    exactly as before (root row IS a wire row).
  * PAYLOAD — ``_runMatrixProbe`` posts ``request_ids`` and ``key_access``
    with each model so the backend resolves the SAME pool the dispatcher
    does (resolve_request_ids).
  * PRUNE — ``_ingestProbeSnapshot`` drops probe cells whose (key × wire id)
    no longer exists in the CURRENT grid (verdicts recorded against
    logical-only ids by pre-fix snapshots, deleted models, removed keys) and
    recomputes the summary — a ghost '✓ reachable' for an id the gateway
    never routes is how fake coverage looks.
  * NEUTER — stripping the explicit-pool branch of ``_modelRowIds`` from a
    COPY flips the row/payload checks red (proves the contract mirror is
    load-bearing); the shipped file stays byte-identical.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_matrix_wire_pool.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
ACCESS_MATRIX_JS = runtime_section_path('settings/providers/access_matrix.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.document = {
  querySelector: function () { return null; },
  querySelectorAll: function () { return []; },
  getElementById: function () { return null; },
};
global.t = function (k) { return k; };
global.escapeHtml = function (s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
};
global.showToast = function () {};
global._renderProvidersTab = function () {};
global._stgProviders = [];
global._maskApiKey = function (k) { return k ? '…' + k.slice(-4) : ''; };
global._detectBrand = function () { return ''; };
global._brandSvg = function () { return ''; };

var _postedBodies = [];
global.Api = {
  providers: {
    probeCellsStart: function (body) {
      _postedBodies.push(body);
      return { then: function () { return { catch: function () {} }; } };
    },
    probeCellsStatus: function () {
      return { then: function () { return { catch: function () {} }; } };
    },
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // REAL access_matrix.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

_stgProviders[0] = {
  id: 'p',
  api_keys: ['sk-aaa', 'sk-bbb'],
  models: [
    // Model-identity contract entry: logical id + explicit wire pool. The
    // cell on key#1 adds a key-restricted deployment AND disables one pool id.
    { model_id: 'claude-x', request_ids: ['aws.claude-x', 'vertex.claude-x'],
      capabilities: ['text'],
      key_access: { '1': { request_ids: ['yuju-claude-x-daily'],
                           disabled_ids: ['aws.claude-x'] } } },
    // Legacy entry: no request_ids → pool is [model_id] + aliases.
    { model_id: 'm-leg', aliases: ['m-leg-fast'], capabilities: ['text'] },
    // In-pool entry: model_id IS one of its own request_ids (the
    // deepseek-v4-flash shape) → it is a genuine wire id, NOT a pure
    // logical name — it must render as the root wire row, never as BOTH a
    // logical header AND a wire row with the same data-id.
    { model_id: 'ds-flash', request_ids: ['ds-flash', 'ds-flash-huawei'],
      capabilities: ['text'] },
  ],
};
_stgMatrixProbeAttached[0] = true;   // skip the resume-from-disk timer

// ── POOL RESOLUTION ───────────────────────────────────────────────────
check('explicit_pool_is_union_entry_plus_cells',
      JSON.stringify(_modelRowIds(_stgProviders[0].models[0])) ===
      '["aws.claude-x","vertex.claude-x","yuju-claude-x-daily"]');
check('explicit_pool_excludes_logical_id',
      _modelRowIds(_stgProviders[0].models[0]).indexOf('claude-x') < 0);
check('legacy_pool_keeps_root_plus_aliases',
      JSON.stringify(_modelRowIds(_stgProviders[0].models[1])) ===
      '["m-leg","m-leg-fast"]');
check('key_pool_cell_override_wins_for_that_key',
      JSON.stringify(_modelKeyPool(_stgProviders[0].models[0], 1)) ===
      '["yuju-claude-x-daily"]');
check('key_pool_inherits_entry_when_cell_has_none',
      JSON.stringify(_modelKeyPool(_stgProviders[0].models[0], 0)) ===
      '["aws.claude-x","vertex.claude-x"]');
check('key_pool_entry_level_when_null',
      JSON.stringify(_modelKeyPool(_stgProviders[0].models[0], null)) ===
      '["aws.claude-x","vertex.claude-x"]');

// ── THE shared judgment: pure-logical iff NO pool routes model_id ──
check('pure_logical_true_for_explicit_pool_excluding_model_id',
      _modelIsPureLogical(_stgProviders[0].models[0]) === true);
check('pure_logical_false_for_legacy_entry',
      _modelIsPureLogical(_stgProviders[0].models[1]) === false);
check('pure_logical_false_when_model_id_in_own_pool',
      _modelIsPureLogical(_stgProviders[0].models[2]) === false);

// ── RENDER: logical header + wire rows ────────────────────────────────
const html = _renderAccessMatrix(0);
check('logical_header_renders_model_id',
      html.indexOf('data-id="claude-x"') >= 0);
check('logical_header_marks_preset_badge', html.indexOf('stg-mx-preset') >= 0);
check('logical_header_row_is_logical_class', html.indexOf('is-logical') >= 0);
check('logical_header_has_global_toggle',
      /data-id="claude-x"[\s\S]{0,900}_toggleModelEnabled\(0,0\)/.test(html));
check('logical_header_has_no_perkey_toggle',
      !/data-id="claude-x"[\s\S]{0,1400}_toggleIdAccess/.test(html));
check('logical_header_has_no_row_probe',
      !/data-id="claude-x"[\s\S]{0,900}_probeMatrixScope/.test(html));
check('logical_header_cells_are_empty_logical',
      html.indexOf('stg-mx-cell logical') >= 0);
check('wire_row_aws_renders', html.indexOf('data-id="aws.claude-x"') >= 0);
check('wire_row_vertex_renders', html.indexOf('data-id="vertex.claude-x"') >= 0);
check('wire_row_cell_only_renders',
      html.indexOf('data-id="yuju-claude-x-daily"') >= 0);
check('wire_row_carries_toggle',
      /data-id="aws\.claude-x"[\s\S]{0,1400}_toggleIdAccess\(0,0,0,&quot;aws\.claude-x&quot;\)/.test(html));
check('wire_row_carries_row_probe',
      /data-id="aws\.claude-x"[\s\S]{0,900}_probeMatrixScope\(0,\{model_ids:\[&quot;aws\.claude-x&quot;\]\}\)/.test(html));
check('cell_only_wire_row_toggle_targets_its_own_id',
      html.indexOf('_toggleIdAccess(0,0,1,&quot;yuju-claude-x-daily&quot;)') >= 0);

// ── NOT-ROUTED cells: an id outside ITS OWN key's pool gets no toggle, no
//    pip, no probe — the dispatcher can never route that pair. ──
// key#0's pool is [aws, vertex] → yuju is not routed via key#0.
check('noroute_cell_renders_for_wrong_key',
      /<td class="stg-mx-cell noroute" data-model="0" data-key-idx="0" data-id="yuju-claude-x-daily"/.test(html));
check('noroute_cell_has_no_toggle',
      html.indexOf('_toggleIdAccess(0,0,0,&quot;yuju-claude-x-daily&quot;)') < 0);
// key#1 replaced its pool with [yuju] → aws/vertex are not routed via key#1.
check('noroute_both_directions',
      /<td class="stg-mx-cell noroute" data-model="0" data-key-idx="1" data-id="aws\.claude-x"/.test(html) &&
      /<td class="stg-mx-cell noroute" data-model="0" data-key-idx="1" data-id="vertex\.claude-x"/.test(html));
// The ✎ override editor survives on the first wire row even when noroute —
// it is the only way to EDIT that key's pool.
check('noroute_first_wire_keeps_editor',
      /<td class="stg-mx-cell noroute" data-model="0" data-key-idx="1" data-id="aws\.claude-x"[\s\S]{0,300}_editMatrixCell\(0,0,1\)/.test(html));
// Legacy entry unchanged: root row IS a wire row with its own toggle/probe.
check('legacy_root_row_is_wire_row',
      /data-id="m-leg"[\s\S]{0,1400}_toggleIdAccess\(0,1,0,&quot;m-leg&quot;\)/.test(html));
check('legacy_alias_row_kept',
      html.indexOf('data-id="m-leg-fast"') >= 0);

// ── IN-POOL model_id: genuine wire id → root wire row, NO logical header,
//    and exactly ONE row carries the data-id in the whole table. ──
check('in_pool_no_logical_row',
      !/<tr class="stg-mx-row[^"]*is-logical[^"]*"[^>]*data-id="ds-flash"/.test(html));
check('in_pool_root_row_carries_toggle',
      /data-id="ds-flash"[\s\S]{0,1400}_toggleIdAccess\(0,2,0,&quot;ds-flash&quot;\)/.test(html));
check('in_pool_root_row_keeps_global_toggle',
      /data-id="ds-flash"[\s\S]{0,900}_toggleModelEnabled\(0,2\)/.test(html));
check('in_pool_other_pool_ids_render_as_subrows',
      html.indexOf('data-id="ds-flash-huawei"') >= 0);
const dupRows = html.match(/<tr class="stg-mx-row[^"]*"[^>]*data-id="ds-flash"/g) || [];
check('no_duplicate_data_id_rows', dupRows.length === 1);

// ── PAYLOAD: request_ids + key_access ride along ─────────────────────
_postedBodies.length = 0;
_runMatrixProbe(0, true);
check('probe_body_posted', _postedBodies.length === 1);
const b0 = _postedBodies[0] || {};
const bm0 = (b0.models || [])[0] || {};
check('payload_carries_request_ids',
      JSON.stringify(bm0.request_ids) === '["aws.claude-x","vertex.claude-x"]');
check('payload_carries_key_access',
      !!bm0.key_access &&
      JSON.stringify((bm0.key_access['1'] || {}).request_ids) === '["yuju-claude-x-daily"]' &&
      JSON.stringify((bm0.key_access['1'] || {}).disabled_ids) === '["aws.claude-x"]');
const bm1 = (b0.models || [])[1] || {};
check('payload_legacy_model_has_no_request_ids_field',
      !bm1.request_ids);
check('payload_legacy_still_carries_aliases',
      JSON.stringify(bm1.aliases) === '["m-leg-fast"]');

// ── PRUNE: stale cells from pre-fix snapshots / deleted rows heal ─────
_ingestProbeSnapshot(0, {
  status: 'done',
  cells: {
    // Fresh verdict on a REAL wire id — must survive.
    '0::aws.claude-x': { key_idx: 0, model_id: 'aws.claude-x',
                         root_model_id: 'claude-x', status: 'ok',
                         detail: 'HTTP 200', recommend_disable: false },
    // Verdict recorded against the LOGICAL id (pre-fix probe) — ghost.
    '0::claude-x': { key_idx: 0, model_id: 'claude-x', root_model_id: 'claude-x',
                     status: 'not_found', detail: 'HTTP 404',
                     recommend_disable: true },
    // Verdict for a model deleted from the provider — ghost.
    '1::gone-model': { key_idx: 1, model_id: 'gone-model',
                       root_model_id: 'gone-model', status: 'ok',
                       detail: 'HTTP 200', recommend_disable: false },
    // Verdict on a REAL wire id but the WRONG key: key#1 replaced its pool
    // with [yuju], so vertex is never routed via key#1 — ghost.
    '1::vertex.claude-x': { key_idx: 1, model_id: 'vertex.claude-x',
                            root_model_id: 'claude-x', status: 'ok',
                            detail: 'HTTP 200', recommend_disable: false },
  },
  summary: { ok: 3, disable: 1 }, total: 4, done_count: 4,
});
const pr = _stgMatrixProbe[0];
check('prune_keeps_fresh_wire_cell',
      !!pr.cells['0::aws.claude-x'] && pr.cells['0::aws.claude-x'].status === 'ok');
check('prune_drops_logical_id_cell', !pr.cells['0::claude-x']);
check('prune_drops_deleted_model_cell', !pr.cells['1::gone-model']);
check('prune_drops_wrong_key_cell', !pr.cells['1::vertex.claude-x']);
check('prune_recomputes_summary',
      pr.summary.ok === 1 && pr.summary.disable === 0);

console.log(out.join('\n'));
process.exit(0);
"""


def _run_harness(matrix_js: str) -> str:
    harness = os.path.join(HERE, '_matrix_wire_pool_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, matrix_js],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_available(), reason='node not installed')
class WirePoolFrontendTest(unittest.TestCase):

    def test_wire_pool_rows_payload_prune(self):
        output = _run_harness(ACCESS_MATRIX_JS)
        fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
        self.assertEqual(fails, [], 'wire-pool frontend failures:\n' + output)
        self.assertGreaterEqual(output.count('PASS'), 25,
                                'expected >=25 PASS lines, got:\n' + output)

    def test_neuter_explicit_pool_branch_is_load_bearing(self):
        """NEUTER: strip the explicit-pool branch of ``_modelRowIds`` from a
        COPY → the matrix falls back to ``[model_id] + aliases`` → the
        logical id becomes a probeable row again and the wire rows vanish →
        the row checks MUST go red. Proves the contract mirror is what keeps
        the matrix honest."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = """function _modelKeyPool(m, keyIdx) {
  var cell = (keyIdx === null || keyIdx === undefined) ? {} : _getCell(m, keyIdx);
  var explicit = _mxDedupe(cell.request_ids || []);
  if (!explicit.length) explicit = _mxDedupe(m.request_ids || []);
  if (explicit.length) return explicit;"""
        self.assertIn(anchor, src, 'explicit-pool anchor drifted — update the neuter')
        neutered = src.replace(
            anchor,
            """function _modelKeyPool(m, keyIdx) {
  var cell = (keyIdx === null || keyIdx === undefined) ? {} : _getCell(m, keyIdx);
  var explicit = _mxDedupe(cell.request_ids || []);
  if (!explicit.length) explicit = _mxDedupe(m.request_ids || []);
  if (false && explicit.length) return explicit;""",
            1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL explicit_pool_is_union_entry_plus_cells', output,
                      'NEUTER did not bite: pool still resolved without the branch.\n'
                      + output)
        self.assertIn('FAIL logical_header_has_no_perkey_toggle', output,
                      'NEUTER did not bite: logical id stayed non-toggleable.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')

    def test_neuter_prune_is_load_bearing(self):
        """NEUTER: strip the ``_pruneProbeCellsToGrid`` call from a COPY →
        ghost cells (logical-id verdicts, deleted models) survive ingest and
        their false recommend-disable stays applicable → prune checks red."""
        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            src = f.read()
        anchor = '  _pruneProbeCellsToGrid(provIdx);\n'
        self.assertIn(anchor, src, 'prune-call anchor drifted — update the neuter')
        neutered = src.replace(anchor, '', 1)
        self.assertNotEqual(neutered, src)

        with tempfile.TemporaryDirectory() as tmp:
            copy = os.path.join(tmp, 'access_matrix_neutered.js')
            with open(copy, 'w', encoding='utf-8') as f:
                f.write(neutered)
            output = _run_harness(copy)
        self.assertIn('FAIL prune_drops_logical_id_cell', output,
                      'NEUTER did not bite: ghost logical-id cell pruned anyway.\n'
                      + output)

        with open(ACCESS_MATRIX_JS, encoding='utf-8') as f:
            self.assertEqual(f.read(), src, 'harness mutated the shipped access_matrix.js')


if __name__ == '__main__':
    unittest.main()
