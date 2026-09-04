"""Behavior and retained-wiring contracts for write-gate refusal presentation."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/conversation/presentation/write-gate-refusal.ts'
OWNER_JS = Path(native_module_path('.native/write-gate-refusal-contract.js', OWNER))
TOOL_ROUNDS = os.path.join(JS_DIR, 'ui', 'tool_rounds.js')

_OWNER_HARNESS = r"""
eval(process.env.OWNER_SOURCE);

const checks = [];
function check(name, condition) {
  checks.push((condition ? 'PASS ' : 'FAIL ') + name);
}

const messages = {
  'tool.gateStaleBadge': 'changed on disk',
  'tool.gateReadFirstBadge': 'must read first',
  'tool.gatePartialStaleBadge': 'partial · changed',
  'tool.gatePartialReadFirstBadge': 'partial · unread',
  'tool.gateContentRefBadge': 'content ref failed',
  'tool.gateTargetGeneric': 'The target file',
  'tool.gateStaleTitle': 'Write blocked — file changed on disk',
  'tool.gateStaleText': '{paths} changed; re-read the file and re-issue.',
  'tool.gateReadFirstTitle': 'Edit blocked — file not read yet',
  'tool.gateReadFirstText': 'Read {paths} with read_files before editing.',
  'tool.gatePartialStaleTitle': '{skipped} edit(s) blocked — changed',
  'tool.gatePartialStaleText':
    '{paths}: {skipped} edit(s) blocked; the other {proceeded} edit(s) ran normally.',
  'tool.gatePartialReadFirstTitle': '{skipped} edit(s) blocked — unread',
  'tool.gatePartialReadFirstText':
    '{paths}: {skipped} edit(s) blocked; the other {proceeded} edit(s) ran normally.',
  'tool.gateContentRefTitle': 'Write not executed — content reference failed',
  'tool.gateContentRefText': 'The content_ref has no result; retry explicit content.',
};
function translate(key, params) {
  let value = messages[key] || key;
  if (!params || typeof params !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
}
function iconHtml(name, size) {
  return '<i data-icon="' + name + '" data-size="' + size + '"></i>';
}

const presentation = createWriteGateRefusalPresentation({ translate, iconHtml });
check('immutable_public_port', Object.isFrozen(presentation));
check('narrow_public_surface',
  typeof presentation.resolveRefusal === 'function'
  && typeof presentation.renderBadgeHtml === 'function'
  && typeof presentation.renderNoticeHtml === 'function'
  && Object.keys(presentation).length === 3);

const frozenRound = Object.freeze({ toolName: 'apply_diffs' });
const frozenMetadata = Object.freeze({
  badge: 'stale',
  refusal: Object.freeze({
    kind: 'stale',
    paths: Object.freeze(['docs/JOURNAL.md']),
  }),
});
const before = JSON.stringify([frozenRound, frozenMetadata]);
const stale = presentation.resolveRefusal(frozenRound, frozenMetadata);
check('structured_refusal_is_normalized_and_frozen',
  stale && stale.kind === 'stale'
  && JSON.stringify(stale.paths) === JSON.stringify(['docs/JOURNAL.md'])
  && Object.isFrozen(stale) && Object.isFrozen(stale.paths));
check('projection_is_not_mutated',
  JSON.stringify([frozenRound, frozenMetadata]) === before);

const staleBadge = presentation.renderBadgeHtml(stale);
check('stale_badge_is_terminal_localized_warning',
  staleBadge.includes('ptool-badge-warn ptool-badge-gate')
  && staleBadge.includes('changed on disk')
  && staleBadge.includes('Write blocked — file changed on disk')
  && !staleBadge.includes('>stale<'));
const staleNotice = presentation.renderNoticeHtml(stale);
check('stale_notice_names_path_and_remedy',
  staleNotice.includes('ptool-gate-note')
  && staleNotice.includes('title="docs/JOURNAL.md"')
  && staleNotice.includes('>JOURNAL.md</code>')
  && staleNotice.includes('re-read the file and re-issue'));
check('notice_uses_trusted_shared_icon_port',
  staleNotice.includes('<i data-icon="shield" data-size="13"></i>'));

const legacyCases = [
  ['stale', 'stale', 'changed on disk'],
  ['read first', 'read_first', 'must read first'],
  ['partial: stale', 'partial_stale', 'partial · changed'],
  ['partial: read first', 'partial_read_first', 'partial · unread'],
  ['ref failed', 'content_ref', 'content ref failed'],
];
check('legacy_badges_map_only_for_write_tools', legacyCases.every(
  ([badge, kind, label]) => {
    const refusal = presentation.resolveRefusal(
      { toolName: 'write_file' },
      { badge },
    );
    return refusal && refusal.kind === kind
      && presentation.renderBadgeHtml(refusal).includes(label);
  },
));
const legacyStale = presentation.resolveRefusal(
  { toolName: 'write_file' },
  { badge: 'stale' },
);
check('legacy_notice_uses_generic_target',
  presentation.renderNoticeHtml(legacyStale).includes('The target file'));
check('badge_collision_and_ordinary_failure_fail_closed',
  presentation.resolveRefusal({ toolName: 'list_dir' }, { badge: 'stale' }) === null
  && presentation.resolveRefusal(
    { toolName: 'apply_diff' }, { badge: 'failed', writeOk: false },
  ) === null);

const readFirst = presentation.resolveRefusal(
  { toolName: 'apply_diff' },
  { refusal: { kind: 'read_first', paths: ['src/a.py'] } },
);
check('read_first_notice_explains_required_read',
  presentation.renderNoticeHtml(readFirst).includes('read_files')
  && presentation.renderBadgeHtml(readFirst).includes('must read first'));
const partial = presentation.resolveRefusal(
  { toolName: 'apply_diffs' },
  {
    refusal: {
      kind: 'partial_stale', paths: ['src/c.py'], skipped: 1, proceeded: 2,
    },
  },
);
const partialNotice = presentation.renderNoticeHtml(partial);
check('partial_refusal_interpolates_typed_counts',
  partial && partial.skipped === 1 && partial.proceeded === 2
  && presentation.renderBadgeHtml(partial).includes('partial · changed')
  && partialNotice.includes('1 edit(s) blocked')
  && partialNotice.includes('the other 2 edit(s) ran normally'));
const contentReference = presentation.resolveRefusal(
  { toolName: 'write_file' },
  { refusal: { kind: 'content_ref' } },
);
check('content_reference_has_dedicated_copy',
  presentation.renderBadgeHtml(contentReference).includes('content ref failed')
  && presentation.renderNoticeHtml(contentReference).includes('retry explicit content'));

const fallbackFromMalformedStructured = presentation.resolveRefusal(
  { toolName: 'edit_file' },
  { badge: 'stale', refusal: { kind: 42 } },
);
check('malformed_structured_value_can_use_legacy_fallback',
  fallbackFromMalformedStructured
  && fallbackFromMalformedStructured.kind === 'stale');
const invalidCounts = presentation.resolveRefusal(
  { toolName: 'apply_diffs' },
  {
    refusal: {
      kind: 'partial_stale',
      paths: ['ok.py', 42, '', null],
      skipped: -1,
      proceeded: 1.5,
    },
  },
);
check('invalid_paths_and_counts_fail_closed',
  invalidCounts
  && JSON.stringify(invalidCounts.paths) === JSON.stringify(['ok.py'])
  && invalidCounts.skipped === 0 && invalidCounts.proceeded === 0);

const future = presentation.resolveRefusal(
  { toolName: 'write_file' },
  { refusal: { kind: 'future<script>' } },
);
check('unknown_kind_is_safe_and_forward_visible',
  presentation.renderBadgeHtml(future).includes('future&lt;script&gt;')
  && presentation.renderNoticeHtml(future) === '');
const hostilePath = presentation.resolveRefusal(
  { toolName: 'write_file' },
  { refusal: { kind: 'stale', paths: ['dir/<script>"x.py'] } },
);
const hostilePathNotice = presentation.renderNoticeHtml(hostilePath);
check('path_markup_is_escaped',
  !hostilePathNotice.includes('<script>')
  && hostilePathNotice.includes('&lt;script&gt;&quot;x.py'));

const hostileCopy = createWriteGateRefusalPresentation({
  translate: () => '<img src=x onerror=alert(1)>',
  iconHtml,
});
const hostileInfo = hostileCopy.resolveRefusal(
  { toolName: 'write_file' },
  { refusal: { kind: 'stale' } },
);
const hostileCopyHtml = hostileCopy.renderBadgeHtml(hostileInfo)
  + hostileCopy.renderNoticeHtml(hostileInfo);
check('translated_copy_is_escaped_but_icon_markup_is_trusted',
  !hostileCopyHtml.includes('<img src=x')
  && hostileCopyHtml.includes('&lt;img src=x')
  && hostileCopyHtml.includes('<i data-icon="shield"'));

check('invalid_inputs_are_empty',
  presentation.resolveRefusal(null, null) === null
  && presentation.renderBadgeHtml(null) === ''
  && presentation.renderNoticeHtml(null) === '');

console.log(checks.join('\n'));
"""

_WIRING_HARNESS = r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatInner"></div></body>',
  targets: [process.argv[4], process.argv[2]],
  globals: {
    _convRenderFingerprint: () => 0,
    conversations: [],
    activeConvId: null,
    t: (key, params) => {
      const values = {
        'tool.gateStaleBadge': 'changed on disk',
        'tool.gateReadFirstBadge': 'must read first',
        'tool.gatePartialStaleBadge': 'partial · changed',
        'tool.gateTargetGeneric': 'The target file',
        'tool.gateStaleTitle': 'Write blocked',
        'tool.gateReadFirstTitle': 'Edit blocked',
        'tool.gatePartialStaleTitle': '{skipped} edit(s) blocked',
        'tool.gateStaleText': '{paths} changed',
        'tool.gateReadFirstText': 'Read {paths} first',
        'tool.gatePartialStaleText':
          '{paths}: {skipped} blocked; {proceeded} proceeded',
      };
      let value = values[key] || key;
      if (!params || typeof params !== 'object') return value;
      return value.replace(/\{(\w+)\}/g, (token, name) => (
        Object.prototype.hasOwnProperty.call(params, name)
          ? String(params[name]) : token
      ));
    },
  },
});

check('entry_exposed', typeof renderToolRoundsHTML === 'function');

const writeHtml = renderToolRoundsHTML([{
  roundNum: 1, toolName: 'write_file', status: 'done',
  query: 'Write JOURNAL.md',
  toolArgs: JSON.stringify({ path: 'JOURNAL.md', content: '# hi\n' }),
  results: [{ badge: 'stale', writeOk: false }],
}], false);
check('write_file_wires_badge_and_notice',
  writeHtml.includes('ptool-badge-gate')
  && writeHtml.includes('ptool-gate-note'));

const singleDiffHtml = renderToolRoundsHTML([{
  roundNum: 2, toolName: 'apply_diff', status: 'done',
  query: 'Patch a.py',
  toolArgs: JSON.stringify({ path: 'a.py', search: 'x', replace: 'y' }),
  results: [{
    badge: 'read first', writeOk: false,
    refusal: { kind: 'read_first', paths: ['a.py'] },
  }],
}], false);
check('single_diff_wires_notice',
  singleDiffHtml.includes('ptool-gate-note')
  && singleDiffHtml.includes('must read first'));

const batchHtml = renderToolRoundsHTML([{
  roundNum: 3, toolName: 'apply_diffs', status: 'done',
  query: 'Patch files',
  toolArgs: JSON.stringify({ edits: [
    { path: 'a.py', search: 'x', replace: 'y', description: 'one' },
    { path: 'c.py', search: 'm', replace: 'n', description: 'two' },
  ] }),
  results: [{
    badge: 'partial: stale', writeOk: true,
    refusal: { kind: 'partial_stale', paths: ['b.py'], skipped: 1, proceeded: 2 },
    editSummaries: [
      { path: 'a.py', description: 'one', status: 'ok', detail: '' },
      { path: 'c.py', description: 'two', status: 'ok', detail: '' },
    ],
  }],
}], false);
check('batch_diff_wires_notice',
  batchHtml.includes('ptool-gate-note')
  && batchHtml.includes('1 edit(s) blocked'));

const nonWriteHtml = renderToolRoundsHTML([{
  roundNum: 4, toolName: 'list_dir', status: 'done', query: 'list',
  results: [{ badge: 'stale' }],
}], false);
check('non_write_badge_does_not_misfire',
  nonWriteHtml.includes('>stale</span>')
  && !nonWriteHtml.includes('ptool-gate-note'));

report();
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_write_gate_refusal_owner_contract() -> None:
    source = OWNER.read_text(encoding='utf-8')
    assert 'runtimeScope' not in source
    assert 'globalThis' not in source
    assert 'window.' not in source
    assert 'document.' not in source

    process = subprocess.run(
        [shutil.which('node'), '-e', _OWNER_HARNESS],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            'OWNER_SOURCE': OWNER_JS.read_text(encoding='utf-8'),
        },
    )
    assert process.returncode == 0, process.stderr
    failures = [
        line for line in process.stdout.splitlines() if line.startswith('FAIL ')
    ]
    assert not failures, process.stdout
    passes = [
        line for line in process.stdout.splitlines() if line.startswith('PASS ')
    ]
    assert len(passes) == 19, process.stdout


def test_retained_write_blocks_delegate_refusal_markup() -> None:
    run_harness(
        target_js=TOOL_ROUNDS,
        body_js=_WIRING_HARNESS,
        extra_targets=[os.path.join(JS_DIR, 'ui', 'streaming_swarm_panel.js')],
        min_pass=5,
        label='write-gate refusal wiring',
    )
