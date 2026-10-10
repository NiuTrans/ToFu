"""Real Research workspace → action → TeX → PDF, with only prior research mocked."""
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import pytest

from tests.research import test_research_workspace as workspace_fixtures
from tests.support import paper_builds as build_fixtures
from tests.support.envelope import v1_shaped
from tests.e2e.test_e2e_smoke import _wait_app_ready, _SENTINEL
from tests.e2e.test_e2e_smoke import _install_llm_stubs, _disable_open_mode_rate_limit  # noqa: F401
from tests.paper.test_paper_build_compiler import _require_tex
from tests.conftest import _dismiss_onboarding_modals
from lib.research.workspace import empty_workspace, save_workspace

pytestmark = pytest.mark.visual
storage = workspace_fixtures.storage
scheduler = build_fixtures.scheduler


def test_research_compiles_saved_sources_and_retains_pdf_after_failure(page, storage, scheduler, monkeypatch, tmp_path, assert_no_js_errors):
    from pypdf import PdfReader
    import lib.research.compilation as compilation
    import lib.paper.builds.service as service
    import routes.api_v1.paper_builds as build_routes
    import lib.research.capabilities as capabilities
    from lib.paper.builds import artifact_repository, document_repository
    from lib.paper.builds.cache import LocalBuildCache
    _require_tex()
    _wait_app_ready(page)
    monkeypatch.setattr(compilation, 'get_scheduler', lambda: scheduler)
    monkeypatch.setattr(service, 'get_scheduler', lambda: scheduler)
    monkeypatch.setattr(build_routes, 'get_scheduler', lambda: scheduler)
    import time as _time

    def _ready_client(**_):
        # The test restarts the private supervisor mid-flight; a reading/compile
        # request can race the restart handshake. Wait out the bounded window
        # instead of failing closed on the first not-ready read.
        deadline = _time.monotonic() + 20
        while True:
            if storage.ready:
                return storage.client
            if _time.monotonic() >= deadline:
                return storage.client  # raises with the authoritative status
            _time.sleep(0.02)

    monkeypatch.setattr(artifact_repository, '_storage', _ready_client)
    monkeypatch.setattr(document_repository, '_storage', _ready_client)
    # No MCP server participates in compilation or discovery in this fixture.
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **kwargs: {
        'contract_version': 'tofu.research-capabilities/v1', 'capabilities': [], 'tools': [], 'fingerprint': ''})
    direction = 'Compile the exact saved research manuscript'
    viewer_requests = []
    page.on('request', lambda request: viewer_requests.append(request.post_data_json)
            if request.method == 'POST' and request.url.endswith('/viewers') else None)
    source = (r'\documentclass{article}\usepackage{hyperref}\begin{document}'
              r'\section{Introduction}Verified Research manuscript.'
              r'\newpage\section{Results}Research results remain readable.'
              r'\newpage\section{Conclusion}A retained manuscript.\end{document}')
    draft = empty_workspace(direction)
    draft['source_files'] = [{'path': 'main.tex', 'content': source}]
    save_workspace(direction, 'en', draft, expected_revision=0, user_id=1)
    # Seed the durable run record directly: HTTP route mocks are a no-op in
    # some environments, while the record store is the authority the real
    # `/api/v1/research/records` endpoints already read.
    from lib.research.records import persist_record
    persist_record({'task_id': 'completed-research-fixture', 'user_id': 1,
                    'direction': direction, 'lang': 'en', 'status': 'done',
                    'result': {'survey_md': 'A saved research fixture.',
                               'accepted': [], 'rejected': []}})
    _dismiss_onboarding_modals(page)
    page.evaluate("() => { window.location.hash = '/evolution/research'; }")
    page.locator('.research-record-item').filter(has_text=direction).click()
    page.locator('[data-rs-tab="paper"]').click()
    workspace = page.locator('#researchWorkspace')
    compile_button = workspace.locator("""button[data-tofu-action^="_startResearchAction('compile'"]""")
    assert compile_button.is_enabled()
    model_calls = _SENTINEL['stream_calls']
    page.once('dialog', lambda dialog: dialog.accept())
    compile_button.click()
    page.wait_for_function("document.querySelector('.rsw-compiler [data-compile-status=passing]')",
                           timeout=60000)
    reader = workspace.locator('#researchManuscriptReader')
    page.wait_for_function("document.querySelector('#researchManuscriptReader canvas') && document.querySelector('#researchManuscriptReader')?.dataset.buildId",
                           timeout=30000)
    reader.scroll_into_view_if_needed()
    reader.get_by_role('spinbutton').fill('2')
    reader.get_by_role('spinbutton').press('Tab')
    reader.locator('.paper-preview-zoom').fill('125%')
    reader.locator('.paper-preview-zoom').press('Tab')
    page.wait_for_function("document.querySelector('#researchManuscriptReader [data-page=\"2\"] canvas')")
    page.evaluate("window.researchReaderViewport = document.querySelector('#researchManuscriptReader .paper-preview-viewport')")
    reader.locator('.paper-preview-toolbar button[aria-controls="researchManuscriptDrawer"]').last.click()
    link = workspace.locator('a[download="manuscript.pdf"]')
    pdf_url = link.get_attribute('href')
    response = page.request.get(urljoin(page.url, pdf_url))
    assert response.status == 200
    assert 'Verified Research manuscript.' in PdfReader(BytesIO(response.body())).pages[0].extract_text()
    original_pdf = response.body()
    reader.locator('.paper-preview-drawer-close').click()
    reader.locator('.paper-preview-toolbar button[aria-pressed]').click()
    assert reader.locator('dialog:modal').is_visible()
    assert reader.get_by_role('spinbutton').input_value() == '2'
    assert reader.locator('.paper-preview-zoom').input_value() == '125%'
    reader.locator('.paper-preview-viewport').press('Escape')
    assert not reader.locator('dialog').count()
    assert page.evaluate("researchReaderViewport === document.querySelector('#researchManuscriptReader .paper-preview-viewport')")
    # Workspace/CAS refresh must keep the same reader and reading position.
    workspace.locator('.rsw-save button').click()
    page.wait_for_function("!document.querySelector('.rsw-save button').disabled")
    assert page.evaluate("researchReaderViewport === document.querySelector('#researchManuscriptReader .paper-preview-viewport')")
    assert reader.get_by_role('spinbutton').input_value() == '2'
    assert sum(item['operation'] == 'retain' for item in viewer_requests) == 1
    assert reader.locator('canvas').evaluate_all('(nodes) => nodes.reduce((sum, node) => sum + node.width * node.height * 4, 0)') <= 64 * 1024 * 1024
    reader.locator('.paper-preview-toolbar button[aria-controls="researchManuscriptDrawer"]').first.click()
    reader.get_by_role('searchbox').fill('Research results')
    reader.get_by_role('searchbox').press('Enter')
    page.wait_for_function("document.querySelector('#researchManuscriptDrawer')?.innerText.includes('2 ·')")
    reader.locator('.paper-preview-drawer-close').click()
    storage.stop()
    storage.start()
    scheduler.cache = LocalBuildCache(tmp_path / 'empty-after-restart', scheduler.budget)
    restored = page.request.get(urljoin(page.url, pdf_url))
    assert restored.status == 200 and restored.body() == original_pdf
    assert _SENTINEL['stream_calls'] == model_calls
    artifacts = Path(__file__).resolve().parents[2] / 'output/playwright/paper-preview'
    artifacts.mkdir(parents=True, exist_ok=True)
    reader.screenshot(path=str(artifacts / 'research-compiled.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    # Wait for the shell's drawer breakpoint transition before clicking.
    page.wait_for_function("document.querySelector('#sidebar').getBoundingClientRect().right <= 0")
    assert workspace.locator('.rsw-output').bounding_box()['width'] <= 390
    reader.scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert reader.locator('.paper-preview-zoom').evaluate('(node) => node.clientWidth >= 54')
    icon_widths = reader.locator('.paper-preview-toolbar svg').evaluate_all(
        '(nodes) => nodes.filter(node => node.getClientRects().length).map(node => node.getBoundingClientRect().width)')
    assert icon_widths == [16, 16, 16]
    reader.locator('.paper-preview-toolbar button[aria-controls="researchManuscriptDrawer"]').last.click()
    with page.expect_download() as download:
        link.click()
    assert download.value.failure() is None
    reader.locator('.paper-preview-drawer-close').click()
    reader.screenshot(path=str(artifacts / 'research-compiled-mobile.png'))
    # Hidden pages release PDF resources and reopen at the same position.
    with page.expect_response(lambda response: response.url.endswith('/viewers')
                             and response.request.post_data_json['operation'] == 'release') as released:
        page.locator('[data-rs-tab="thread"]').click()
    assert released.value.json()['data']['retained'] is False
    assert workspace.locator('#researchManuscriptReader').count() == 0
    page.locator('[data-rs-tab="paper"]').click()
    page.wait_for_function("document.querySelector('#researchManuscriptReader [data-page=\"2\"] canvas')")
    assert reader.locator('.paper-preview-zoom').input_value() == '125%'
    page.evaluate("window.researchReaderViewport = document.querySelector('#researchManuscriptReader .paper-preview-viewport')")
    page.set_viewport_size({'width': 1280, 'height': 720})

    # Real refresh restores server-owned position/mode before painting this PDF.
    with page.expect_response(lambda response: response.url.endswith('/reading')
                             and response.request.post_data_json['state']['reading_mode']) as saved:
        reader.locator('.paper-preview-toolbar button[aria-pressed]').click()
    assert saved.value.status == 200
    page.reload()
    _wait_app_ready(page)
    _dismiss_onboarding_modals(page)
    if not page.locator('#researchViewer').is_visible():
        page.evaluate("() => { window.location.hash = '/evolution/research'; }")
    page.locator('.research-record-item').filter(has_text=direction).click()
    page.locator('[data-rs-tab="paper"]').click()
    page.wait_for_function("document.querySelector('#researchManuscriptReader [data-page=\"2\"] canvas')")
    assert reader.get_by_role('spinbutton').input_value() == '2'
    assert reader.locator('.paper-preview-zoom').input_value() == '125%'
    assert reader.locator('dialog:modal').is_visible()
    assert not reader.locator('.paper-preview-notice').is_visible()
    assert _SENTINEL['stream_calls'] == model_calls
    reader.locator('.paper-preview-viewport').press('Escape')
    page.evaluate("window.researchReaderViewport = document.querySelector('#researchManuscriptReader .paper-preview-viewport')")

    workspace.locator('textarea[data-rsw-field="source_files.0.content"]').fill(
        source.replace('Verified Research manuscript.', r'\undefinedResearchCommand'))
    page.once('dialog', lambda dialog: dialog.accept())
    compile_button.click()
    page.wait_for_function("document.querySelector('.rsw-compiler [data-compile-status=failing]')",
                           timeout=60000)
    assert page.evaluate("researchReaderViewport === document.querySelector('#researchManuscriptReader .paper-preview-viewport')")
    assert reader.get_by_role('spinbutton').input_value() == '2'
    assert sum(item['operation'] == 'retain' for item in viewer_requests) == 3
    reader.locator('.paper-preview-toolbar button[aria-controls="researchManuscriptDrawer"]').last.click()
    assert link.get_attribute('href') == pdf_url
    assert page.request.get(urljoin(page.url, pdf_url)).status == 200
    assert _SENTINEL['stream_calls'] == model_calls
    final = v1_shaped(page.request.get(urljoin(page.url, '/api/v4/research/workspace'), params={'direction': direction, 'lang': 'en'}).json())
    assert not next(row['ok'] for row in final['readiness']['gates'] if row['id'] == 'compile')
    assert workspace.locator('.rsw-output [data-gate-id]').count() == len(final['readiness']['gates'])
    assert not workspace.locator('.rsw-output [data-gate-id="compile"]').get_attribute('class')
    (artifacts / 'research-compile-evidence.json').write_text(json.dumps({
        'status': 'verified', 'build': final['workspace']['compilation']['build'],
        'latest_attempt': final['workspace']['compilation']['status'], 'model_calls': 0,
        'pdf_preserved_after_failure': True,
        'pdf_restored_after_storage_restart_and_cache_loss': True,
        'embedded_reader': True, 'reading_preserved_on_workspace_update': True,
        'reading_position_zoom_mode_restored_after_refresh': True,
        'hidden_reader_released_and_reopened': True, 'all_server_gates_presented': True}, indent=2) + '\n')
    reader.locator('.paper-preview-drawer-close').click()
    with page.expect_response(lambda response: response.url.endswith('/viewers')
                             and response.request.post_data_json['operation'] == 'release') as released:
        page.locator('[data-rs-tab="thread"]').click()
    assert released.value.json()['data']['retained'] is False
