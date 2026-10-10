"""Real-browser delivery projection on the locally served, built application.

Only read API responses are controlled; the app shell, lazy Project Brain
bundle, locale catalogs, CSS and public feature command are production code.
No renderer injection or source rewriting. Git delivery itself is covered by
storage/integration tests; this spec proves the visible read-model boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

pytestmark = [pytest.mark.visual, pytest.mark.slow]
ROOT = Path(__file__).resolve().parents[2]
STATES = ('pending', 'blocked', 'failed', 'preserved', 'delivered', None, 'bogus')
REASON = ('<img src=x onerror="globalThis.__deliveryXss=1"> & "quoted" '
          '<script>globalThis.__deliveryXss=2</script>')
HEAD = '<b>&"sha</b>'
TARGET = '<i>&"tar</i>'
UPDATED = '<svg onload="globalThis.__deliveryXss=3">'


def _payload(project_path: str) -> dict:
    workspaces = []
    for index, state in enumerate(STATES):
        row = {
            'workId': f'delivery-{index}',
            'title': f'{state or "legacy"} delivery evidence',
            'workspacePath': f'{project_path}/writer-{index}',
            'state': 'merged',
            'checkpointSha': 'a' * 40,
            'dirty': {'total': 0},
        }
        if state is not None:
            row['delivery'] = {
                'state': state, 'reason': REASON, 'headSha': HEAD,
                'targetSha': TARGET, 'updatedAt': UPDATED,
            }
        workspaces.append(row)
    return {
        'ok': True, 'autorun': False,
        'repo': {
            'root': project_path, 'canonicalClean': True,
            'worktreesTotal': 8, 'dirty': {'total': 0},
        },
        'refs': {
            'candidate': 'a' * 40, 'stable': 'b' * 40,
            'candidateInitialized': True, 'stableInitialized': True,
            'candidateAheadStable': 1, 'stableAheadCandidate': 0,
            'headAheadCandidate': 0, 'candidateAheadHead': 1,
        },
        'counts': {'merged': len(workspaces)},
        'server': {}, 'gates': {'builtIn': ['git diff --check']},
        'workspaces': workspaces, 'events': [],
    }


def _locale(language: str) -> dict:
    return json.loads((ROOT / 'frontend/src/i18n/locales' / f'{language}.json')
                      .read_text(encoding='utf-8'))


@pytest.mark.parametrize('language', ['en', 'zh'])
@pytest.mark.parametrize('width,height', [(1440, 1100), (390, 844)],
                         ids=['desktop', 'mobile'])
def test_candidate_merge_is_not_checkout_delivery(
    page, live_server, tmp_path, screenshot_dir, language, width, height,
):
    """All five receipts plus old/unknown metadata stay truthful and inert."""
    from playwright.sync_api import expect

    screenshot_dir = Path(screenshot_dir)
    assert urlparse(live_server).hostname in ('127.0.0.1', 'localhost', '::1'), (
        'This test may only use the isolated local application fixture')
    assert (ROOT / 'static/vite/manifest.json').is_file(), (
        'Build this worktree first; do not test another checkout or inject JS')
    catalog = _locale(language)
    key = 'projectBrain.integration.'
    merged_label = catalog[key + 'state.candidateMerged']
    labels = {state: catalog[key + 'delivery.' + state] for state in (
        'pending', 'blocked', 'failed', 'preserved', 'delivered', 'unknown')}
    # This path is test-owned and never points at the active repository.
    project_path = str(tmp_path / 'delivery-project')
    Path(project_path).mkdir()
    payload = _payload(project_path)
    status_requests = []
    unexpected_project_requests = []
    read_projections = {
        '/api/v4/project/board': {'active': [], 'recentOutcomes': []},
        '/api/v4/project/feed': {'events': []},
        '/api/v4/project/charter': {'decisions': []},
        '/api/v4/project/brain/checkers': {'items': []},
        '/api/v4/project/brain/status': {},
        '/api/v4/project/brain/watch': {'items': []},
    }

    def project_response(route):
        request = route.request
        url = urlparse(request.url)
        if request.method != 'GET':
            unexpected_project_requests.append(f'{request.method} {url.path}')
            route.fulfill(status=405, json={'ok': False})
        elif url.path == '/api/v4/project/integration/status':
            status_requests.append(parse_qs(url.query).get('path'))
            route.fulfill(json=payload)
        elif url.path in read_projections:
            route.fulfill(json=read_projections[url.path])
        else:
            unexpected_project_requests.append(f'{request.method} {url.path}')
            route.fulfill(status=404, json={'ok': False})

    page.route('**/api/v4/project/**', project_response)
    page.set_viewport_size({'width': width, 'height': height})
    page.evaluate('(language) => localStorage.setItem("tofu_ui_lang", language)', language)
    page.reload(wait_until='domcontentloaded')
    _wait_ready(page)
    expect(page.locator('html')).to_have_attribute(
        'lang', 'zh-CN' if language == 'zh' else 'en')
    _invoke_action(page, 'openProjectBrain', {'path': project_path, 'tab': 'charter'})
    tab = page.locator('#projectBrainTabs [data-pb-tab="integration"]')
    tab.click()
    expect(tab).to_have_attribute('aria-selected', 'true')
    host = page.locator('#projectBrainIntegrationBody')
    rows = host.locator('.pbi-writer')
    expect(rows).to_have_count(len(STATES))
    expect(host).not_to_have_attribute('aria-busy', 'true')
    stem = f'project-brain-delivery-{language}-{width}'
    page.screenshot(path=str(screenshot_dir / f'{stem}-overview.png'))

    for index, state in enumerate(STATES):
        row = rows.nth(index)
        row.scroll_into_view_if_needed()
        expected_state = state if state in labels else 'unknown'
        label = row.locator('.pbi-writer-state')
        expect(label).to_have_text(merged_label + ' · ' + labels[expected_state])
        expect(label).to_be_visible()
        if state != 'delivered':
            assert labels['delivered'] not in row.inner_text()
        if state is not None:
            if state != 'delivered':
                expect(row.locator('.pbi-writer-error')).to_have_text(REASON)
            else:
                expect(row.locator('.pbi-writer-error')).to_have_count(0)
            # Git evidence is a tooltip, not duplicate visible status text.
            detail = row.locator('.pbi-writer-meta [title]').get_attribute('title')
            assert REASON in detail and HEAD in detail and TARGET in detail
            # Malformed timestamps may render Invalid Date, never markup.
            assert UPDATED not in row.inner_html()
        assert row.locator('script,img,svg[onload],[onerror]').count() == 0
        assert row.locator('[data-pbi-action]').count() == 0, (
            'Terminal candidate-merged rows must remain non-mutating')
        assert row.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1'), (
            f'{language}/{width}/{state}: writer content overflows horizontally')
        row.screenshot(path=str(screenshot_dir / f'{stem}-{state or "legacy"}.png'))

    assert page.evaluate('globalThis.__deliveryXss === undefined')
    assert 'projectBrain.integration.' not in host.inner_text()
    assert host.evaluate('(el) => el.scrollWidth <= el.clientWidth + 1')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    assert status_requests and all(path == [project_path] for path in status_requests)
    assert not unexpected_project_requests, unexpected_project_requests
    assert not getattr(page, '_tofu_js_errors', []), getattr(page, '_tofu_js_errors', [])
    page.screenshot(path=str(screenshot_dir / f'{stem}-receipts.png'))
    _invoke_action(page, 'closeProjectBrain')
    expect(page.locator('#projectBrainOverlay')).to_be_hidden()
