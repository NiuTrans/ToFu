"""Co-Pilot recent projects: the retained host feeds and answers the shared rail.

The rail (``frontend/src/features/project/workspace-rail.ts``) renders recent
rows, filters, counts, highlights, escapes and routes clicks; its Vitest suite
owns that. ``project.js`` owns what this suite drives under jsdom through the
REAL composed section and the REAL rail:

  • ``renderRecentProjects()`` turns ``Api.project.recentList()`` (an array or
    ``{projects}``) into rail rows — invalid entries dropped, counts coerced,
    ``exists: false`` kept as a relinkable missing row; a failed read shows the
    rail's error with a retry that reads again.
  • A picked row reaches ``selectRecentProject`` with its exact stored path:
    staged once, then the folder browser navigates there. A missing row (or a
    pick of a path the host knows is missing) arms relink for that exact path
    and browses its parent instead of staging.
  • Clear calls the server and reloads; opening the modal resets the recent
    filter and reloads; the rail is mounted once into ``.pm-body`` and follows
    UI language switches.

Recent rows bind click closures over the stored path, so a directory name is
never parsed as an action expression (the escaping class the former
``data-tofu-action`` rows had); the crafted paths below still prove the exact
string survives the host round trip.
"""

from __future__ import annotations

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_PROJECT_JS = os.path.join(JS_DIR, 'project.js')

CRAFTED_PATHS = [
    "/tmp/x',deleteEverything(),'",
    '/tmp/quote"and\\backslash',
    "/srv/plain project",
]
MISSING_PATHS = [
    "/old/x',deleteEverything(),'",
    '/old/quote"and\\backslash',
]

_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const CRAFTED = JSON.parse(process.argv[4]);
const MISSING = JSON.parse(process.argv[5]);
const calls = { recentList: 0, recentClear: 0, browse: [] };
let recentReply = () => ({ projects: [] });
const { window, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="projectModal"><div class="modal project-modal pm-workbench">' +
    '<span id="pmMobileCount"></span><div class="pm-body">' +
    '<div class="pm-browser-pane"><div id="remoteDevicesSection" style="display:none"></div></div>' +
    '</div><div id="projectModalHint"></div></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    runtimeScope: {},
    showToast: () => {},
    t: (k, o) => (o && o.path != null ? k + ':' + o.path : k),
    Api: { project: {
      recentList: async () => { calls.recentList += 1; return recentReply(); },
      recentClear: async () => { calls.recentClear += 1; return { ok: true }; },
      browse: (path) => {
        calls.browse.push(path);
        return Promise.resolve({ path, parent: '/', filesCount: 0, dirs: [] });
      },
    } },
  },
});
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const rail = (sel) => document.querySelector('#projectModal .pm-rail ' + sel);
const rows = () => [...document.querySelectorAll(
  '#projectModal .pm-rail .recent-paths-list > .recent-path-item')];
const rowFor = (path) => rows().find((row) => row.dataset.path === path);
const roots = () => [...document.querySelectorAll(
  '#projectModal .pm-rail .mp-folder-list > li.mp-row[data-path]')].map((li) => li.dataset.path);
const lastBrowse = () => calls.browse[calls.browse.length - 1];
const hintText = () => {
  const el = document.querySelector('#projectModalHint .pm-hint-relink .pm-hint-text');
  return el ? el.textContent : '';
};
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ── Feed: Api.project.recentList → rail rows ──
recentReply = () => ({ projects: [
  { path: '/srv/alpha', count: '4' },
  null,
  { path: 42 },
  { count: 5 },
  { path: '/srv/beta', count: 'x' },
  { path: '/srv/gone', count: 9, exists: false },
] });
await renderRecentProjects();
_mpRenderTags();
await renderRecentProjects();
check('rail_mounted_once_in_body',
  document.querySelectorAll('#projectModal .pm-rail').length === 1
  && !!document.querySelector('#projectModal .pm-body > .pm-rail'));
check('feed_keeps_valid_entries_in_order',
  same(rows().map((row) => row.dataset.path), ['/srv/alpha', '/srv/beta', '/srv/gone']));
check('feed_coerces_count',
  (rowFor('/srv/alpha').querySelector('.recent-path-count') || {}).textContent === '×4'
  && !rowFor('/srv/beta').querySelector('.recent-path-count'));
check('feed_keeps_missing_row_relinkable',
  rowFor('/srv/gone').classList.contains('recent-path-missing') && !rowFor('/srv/gone').disabled);
recentReply = () => [{ path: '/srv/alpha' }];
await renderRecentProjects();
check('feed_accepts_bare_array', same(rows().map((row) => row.dataset.path), ['/srv/alpha']));
recentReply = () => null;
await renderRecentProjects();
check('feed_empty_reply_hides_card', rows().length === 0 && rail('.recent-paths').hidden);

// ── Pick: exact path staged once, browser navigates there ──
recentReply = () => ({ projects: [
  ...CRAFTED.map((path) => ({ path, count: 2 })),
  ...MISSING.map((path) => ({ path, exists: false })),
] });
await renderRecentProjects();
const navigated = [];
for (const path of CRAFTED) {
  rowFor(path).click();
  await flush();
  navigated.push(lastBrowse());
}
check('pick_stages_exact_paths_in_order', same(roots(), CRAFTED));
check('pick_navigates_to_exact_path', same(navigated, CRAFTED));
rowFor(CRAFTED[0]).click();
await flush();
check('pick_staged_again_navigates_without_duplicate',
  same(roots(), CRAFTED) && lastBrowse() === CRAFTED[0]);

// ── Missing: relink armed for the exact path, browser lands on its parent ──
const relinked = [];
for (const path of MISSING) {
  rowFor(path).click();
  await flush();
  relinked.push([hintText(), lastBrowse()]);
}
check('missing_click_arms_relink_for_exact_path',
  same(relinked, MISSING.map((path) => ['pm.relinkBanner:' + path, '/old'])));
check('missing_click_never_stages', same(roots(), CRAFTED));
cancelRelinkRecent();
selectRecentProject(MISSING[1]);
check('select_missing_routes_to_relink',
  hintText() === 'pm.relinkBanner:' + MISSING[1] && same(roots(), CRAFTED));
cancelRelinkRecent();

// ── Failed read: rail error + retry that reads again ──
recentReply = () => { throw new Error('disk gone'); };
await renderRecentProjects();
check('read_failure_shows_error_with_retry',
  !rail('.recent-paths').hidden && !rail('.recent-paths-error').hidden
  && rail('.recent-paths-error').textContent === 'disk gone'
  && !rail('.recent-paths-retry').hidden);
recentReply = () => { throw {}; };
await renderRecentProjects();
check('read_failure_without_message_uses_fallback',
  rail('.recent-paths-error').textContent === 'folder.settings.recentFailed');
recentReply = () => ({ projects: [{ path: '/srv/alpha' }] });
const beforeRetry = calls.recentList;
rail('.recent-paths-retry button').click();
await flush();
check('retry_refetches_and_recovers',
  calls.recentList === beforeRetry + 1 && rail('.recent-paths-error').hidden
  && rail('.recent-paths-retry').hidden && same(rows().map((row) => row.dataset.path), ['/srv/alpha']));

// ── Clear: server first, then reload ──
recentReply = () => ({ projects: [] });
const beforeClear = calls.recentList;
rail('.recent-paths-clear').click();
await flush();
check('clear_button_clears_then_reloads',
  calls.recentClear === 1 && calls.recentList === beforeClear + 1 && rail('.recent-paths').hidden);

// ── Open: the recent filter resets and recents reload ──
recentReply = () => ({ projects: [{ path: '/srv/alpha' }, { path: '/srv/beta' }] });
await renderRecentProjects();
const filter = rail('.recent-search-input');
filter.value = 'no-such-project';
filter.dispatchEvent(new window.Event('input', { bubbles: true }));
const filtered = rows().length;
const beforeOpen = calls.recentList;
openProjectModal();
await flush();
check('open_resets_filter_and_refetches',
  filtered === 0 && filter.value === '' && calls.recentList === beforeOpen + 1 && rows().length === 2);

// ── Language switch: the page-lived rail relabels ──
global.t = window.t = (k) => 'zh:' + k;
window.dispatchEvent(new window.Event('tofu:language-change'));
check('language_change_relabels_rail',
  rail('.recent-paths .pm-section-label > span').textContent === 'zh:pm.recent');

report();
process.exit(0);
})().catch((error) => { console.error(error); process.exit(1); });
"""


def test_copilot_recent_projects_feed_pick_relink_retry_and_clear():
    run_harness(
        _PROJECT_JS, _BODY,
        extra_targets=[json.dumps(CRAFTED_PATHS), json.dumps(MISSING_PATHS)],
        expect_pass=18, label='co-pilot recent projects host',
    )
