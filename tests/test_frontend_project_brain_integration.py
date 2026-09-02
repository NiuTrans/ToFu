"""jsdom contract for the visible deterministic integration center.

Covers three regression classes that ACTUALLY happened:
  1. render contract (operational truth visible, escaped, actionable);
  2. the i18n seam — the panel once kept a private en/zh dict instead of the
     locale packs, so the tab never participated in the app language system;
  3. icon names — five glyphs the panel asked for did not exist in the icon
     registry and silently rendered blank buttons/stages.
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><html lang="en"><body>' +
    '<div id="projectBrainOverlay"></div>' +
    '<span id="pbTabCountIntegration" hidden></span>' +
    '<div id="projectBrainIntegrationBody"></div></body></html>',
  targets: [process.argv[2]],
  globals: {
    confirm: () => true,
    ProjectBrain: { _reportFailure: () => {} },
    Api: { project: {} },
  },
});

window.ProjectBrainIntegration.renderIntegration({
  ok: true,
  autorun: true,
  repo: {
    root: '/work/repo', canonicalClean: false, worktreesTotal: 4,
    prunableWorktrees: 1,
    unregisteredWorktreesCount: 1,
    unregisteredWorktrees: [
      { path: '/tmp/old-writer', head: 'eeeeeeeeeeee', prunable: true },
    ],
    dirty: { modified: 12, deleted: 1, untracked: 23, total: 36 },
  },
  refs: {
    candidate: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    stable: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
    candidateInitialized: true, stableInitialized: true,
    candidateAheadStable: 2, stableAheadCandidate: 0,
    headAheadCandidate: 25, candidateAheadHead: 2, headCandidateDiverged: true,
  },
  counts: { quarantined: 1, merged: 2 },
  server: {
    sameRepository: true, servesStable: false, servesCandidate: false,
    codeFingerprint: { dirty: true, digest: 'ccddeeff0011' },
  },
  gates: {
    builtIn: ['git diff --check', 'Python syntax'],
    testCommandConfigured: false, stableCommandConfigured: false,
  },
  workspaces: [{
    taskId: 'writer-1', title: 'Auth refresh', workspacePath: '/work/writer-1',
    state: 'quarantined', checkpointSha: 'dddddddddddddddddddddddddddddddddddddddd',
    error: 'CONFLICT <unsafe>', dirty: { total: 3 }, updatedAt: '2026-08-08T08:00:00Z',
  }, {
    taskId: 'merged-1', title: 'Already merged', workspacePath: '/work/merged-1',
    state: 'merged', checkpointSha: 'ffffffffffffffffffffffffffffffffffffffff',
    error: '', dirty: { total: 0 }, updatedAt: '2026-08-08T08:02:00Z',
  }, {
    taskId: 'ready-1', title: 'Queued safely', workspacePath: '/work/ready-1',
    state: 'ready', checkpointSha: '1111111111111111111111111111111111111111',
    error: '', dirty: { total: 0 }, updatedAt: '2026-08-08T08:03:00Z',
  }],
  events: [{
    id: 1, taskId: 'writer-1', kind: 'quarantined',
    message: 'Checkpoint needs attention', detail: 'same file changed',
    createdAt: '2026-08-08T08:01:00Z',
  }],
});

const host = document.getElementById('projectBrainIntegrationBody');
const text = host.textContent;
check('pipeline_is_visible', text.includes('Writers') && text.includes('Candidate') && text.includes('Stable'));
check('canonical_dirt_is_explicit', text.includes('Canonical dirty') && text.includes('12 modified') && text.includes('23 untracked'));
check('dirty_is_separated_from_refs', text.includes('not part of candidate or stable'));
check('head_candidate_divergence_is_explicit', text.includes('Canonical HEAD and candidate diverged'));
check('running_server_identity_is_visible', text.includes('loaded from dirty local source') && text.includes('ccddeeff0011'));
check('quarantine_reason_is_visible_and_escaped', text.includes('CONFLICT <unsafe>') && host.innerHTML.indexOf('CONFLICT &lt;unsafe&gt;') !== -1);
check('action_badge_counts_dirt_quarantine_and_hygiene', document.getElementById('pbTabCountIntegration').textContent === '3');
check('promotion_and_writer_controls_exist', !!host.querySelector('[data-pbi-action="promote"]') && !!host.querySelector('[data-pbi-action="retry"]') && !!host.querySelector('#pbiCreateForm'));
check('head_reconcile_control_is_visible_for_divergence',
  !!host.querySelector('[data-pbi-action="reconcile-head"]'));
check('terminal_merged_row_has_no_mutating_controls',
  !host.querySelector('[data-task="merged-1"]') &&
  !!host.querySelector('[data-pbi-action="discard"][data-task="writer-1"]'));
check('ready_row_can_be_cancelled_before_worker_claim',
  !!host.querySelector('[data-pbi-action="discard"][data-task="ready-1"]'));
check('unregistered_worktree_is_attributable', text.includes('Unregistered worktrees') && text.includes('/tmp/old-writer') && text.includes('prunable'));
report();
'''

_BODY_I18N = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
// A minimal stand-in for the app i18n seam: every projectBrain.integration.*
// key resolves to a Chinese string, anything else returns the key itself
// (exactly how the real t() signals a missing key).
const ZH = {
  'projectBrain.integration.title': '自动集成控制台',
  'projectBrain.integration.workspaces': 'Writer 工作区',
  'projectBrain.integration.state.quarantined': '已隔离',
  'projectBrain.integration.checkpoint': '保存检查点',
};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><html lang="en"><body>' +
    '<div id="projectBrainOverlay"></div>' +
    '<span id="pbTabCountIntegration" hidden></span>' +
    '<div id="projectBrainIntegrationBody"></div></body></html>',
  targets: [process.argv[2]],
  globals: {
    confirm: () => true,
    ProjectBrain: { _reportFailure: () => {} },
    Api: { project: {} },
    Icon: (name) => '<svg data-icon="' + name + '"></svg>',
    t: (key) => ZH[key] || key,
  },
});

window.ProjectBrainIntegration.renderIntegration({
  ok: true, autorun: false,
  repo: { root: '/work/repo', canonicalClean: true, worktreesTotal: 1, dirty: { total: 0 } },
  refs: { candidate: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', stable: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', candidateInitialized: true, stableInitialized: true },
  counts: {}, server: {}, gates: { builtIn: [] },
  workspaces: [{ taskId: 'w1', title: 'T', workspacePath: '/w1', state: 'quarantined', checkpointSha: 'dddddddddddd', dirty: { total: 0 } }],
  events: [],
});

const host = document.getElementById('projectBrainIntegrationBody');
const text = host.textContent;
// Locale-pack strings WIN over the inline English fallback…
check('title_comes_from_locale_pack', text.includes('自动集成控制台') && !text.includes('Integration control'));
check('state_label_comes_from_locale_pack', text.includes('已隔离'));
check('checkpoint_meta_uses_locale_pack', text.includes('保存检查点'));
// …while a key the pack does NOT translate falls back to inline English,
// never to a raw 'projectBrain.integration.*' key on screen.
check('missing_key_falls_back_to_english_not_raw_key',
  text.includes('Writer workspaces') || text.includes('Writer 工作区'));
check('no_raw_i18n_key_leaks', !text.includes('projectBrain.integration.'));
// Icons flow through the Icon() seam for every stage/action affordance.
check('icons_requested_for_pipeline_and_actions',
  host.innerHTML.includes('data-icon="layers"') &&
  host.innerHTML.includes('data-icon="gitMerge"') &&
  host.innerHTML.includes('data-icon="shield"'));
report();
'''


def test_integration_center_renders_operational_truth() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'project-brain-integration.js'),
        body_js=_BODY,
        expect_pass=12,
    )


def test_integration_center_uses_app_i18n_seam() -> None:
    run_harness(
        target_js=os.path.join(JS_DIR, 'project-brain-integration.js'),
        body_js=_BODY_I18N,
        expect_pass=6,
    )


def test_integration_icons_exist_in_registry() -> None:
    """Every Icon() name the panel requests must resolve in core/icons.js.

    Anchor: 2026-08-21 audit — layers/gitMerge/shield/checkCircle/refreshCw
    were requested but absent from _PATHS, so pipeline stages and the
    promote/submit/refresh buttons rendered with NO glyph (Icon() returns ''
    for unknown names — silent by design, so only a source cross-check bites).
    """
    integration = runtime_section('project-brain-integration.js')
    icons = runtime_section('core/icons.js')
    # Icon names reach _icon() three ways: direct calls, ternary branches
    # (_icon(clean ? 'a' : 'b', n)) and the pipeline stages array ('x'],).
    requested = (
        set(re.findall(r"_icon\('([A-Za-z]+)'", integration))
        | set(re.findall(r"_icon\([^\n']*'([A-Za-z]+)'", integration))
        | set(re.findall(r":\s*'([A-Za-z]+)'\s*,\s*\d+\)", integration))
        | set(re.findall(r",\s*'([A-Za-z]+)'\]", integration))
    )
    assert requested, 'panel requests no icons — the cross-check lost its subject'
    assert {'layers', 'clock', 'gitMerge', 'shield', 'checkCircle',
            'alertTriangle', 'save', 'refreshCw', 'plus'} <= requested, (
        f'extraction drifted — known call sites no longer covered: {sorted(requested)}')
    registry = set(re.findall(r'^\s{4}([A-Za-z]+):', icons, re.M))
    missing = requested - registry
    assert not missing, f'icon names requested but absent from the registry: {sorted(missing)}'


def test_integration_status_poll_is_bounded_to_thirty_seconds() -> None:
    source = runtime_section('project-brain-integration.js')
    assert '}, 30000);' in source
    assert '}, 8000);' not in source
