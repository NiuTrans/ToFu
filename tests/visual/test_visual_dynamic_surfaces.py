"""Production renderer and authenticated resource HTTP boundary in a real browser.

Only ownership and the external compute provider are fixtures. Layout, reads,
snapshot validation, filtering, failure display and teardown use production code.
Screenshots explicitly label illustrative values; no remote jobs are submitted.
"""
import asyncio
import copy
import json
from pathlib import Path
import socket
import subprocess
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from quart import Quart, Response, g, request
from hypercorn.asyncio import serve
from hypercorn.config import Config

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual


@pytest.fixture(scope='module')
def surface_server(tmp_path_factory):
    from unittest.mock import patch
    from lib import response_surfaces as service
    from lib.response_surfaces import mcp
    from tofu_environments import host
    from routes.response_assets import response_assets_bp
    directory = tmp_path_factory.mktemp('dynamic-surface')
    bundle = directory / 'dynamic-response.bundle.js'
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
                    str(ROOT / 'designs/ui-gallery/dynamic-response.ts'), '--bundle', '--format=esm',
                    '--platform=browser', '--outfile=' + str(bundle)], cwd=ROOT, check=True)
    state = {'reads': 0, 'available': 8, 'failure': False}
    def scope(value, *, user_id):
        if user_id != 7 or value != {'runId': 'owned'}:
            raise PermissionError('Owned run required')
        return '/owned/project'
    def catalog(provider, **_kw):
        state['reads'] += 1
        if state['failure']:
            raise RuntimeError('private provider details')
        return {'provider': provider, 'queues': [
            {'name': 'research.training', 'resources': [{'kind':'gpu','name':'H200','unit':'count',
                'total':64,'allocated':64-state['available'],'available':state['available'],'pending':16}]},
            {'name': 'research.shared', 'resources': [{'kind':'gpu','name':'A100','unit':'count',
                'total':32,'allocated':32,'available':0}]}]}
    app = Quart(__name__);app.register_blueprint(response_assets_bp)
    @app.before_request
    async def identity():
        owner = request.cookies.get('surface-owner')
        if owner:
            g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner), tenant_id='test', key_id='test', scopes=[])
    @app.route('/')
    async def shell():
        return (ROOT / 'designs/ui-gallery/dynamic-response.html').read_text().replace('../../static/styles.css','/styles.css').replace('../../frontend/src/styles/fonts.css','/fonts.css')
    @app.route('/dynamic-response.bundle.js')
    async def script(): return Response(bundle.read_bytes(), content_type='text/javascript')
    @app.route('/dynamic-response.fixture.json')
    async def fixture(): return Response((ROOT / 'designs/ui-gallery/dynamic-response.fixture.json').read_bytes(), content_type='application/json')
    @app.route('/styles.css')
    async def style(): return Response((ROOT / 'static/styles.css').read_bytes(), content_type='text/css')
    @app.route('/fonts.css')
    async def fonts(): return Response('', content_type='text/css')
    @app.route('/favicon.ico')
    async def icon(): return '', 204
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0));port = probe.getsockname()[1]
    config = Config();config.bind = [f'127.0.0.1:{port}'];config.accesslog=None;config.errorlog=None
    stop = Event()
    async def shutdown():
        while not stop.is_set(): await asyncio.sleep(0.03)
    with patch.object(service, 'resolve_scope', scope), patch.object(service, 'read_catalog', catalog), \
            patch.object(host,'configured_compute_providers',lambda:()), \
            patch.object(mcp,'providers',lambda owner:[{'id':'queue','connection':'capacity','requiresQueue':True}]), \
            patch.object(mcp,'read_catalog',lambda connection,provider,**kw:catalog(provider,**kw)):
        thread = Thread(target=lambda: asyncio.run(serve(app, config, shutdown_trigger=shutdown)), daemon=True);thread.start()
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1',port), timeout=.1): break
            except OSError: time.sleep(.03)
        try: yield f'http://127.0.0.1:{port}', state
        finally: stop.set();thread.join(timeout=5);assert not thread.is_alive()


@pytest.mark.parametrize('theme', ['tofu', 'dark', 'light'])
@pytest.mark.parametrize('width', [1280, 320])
def test_dynamic_resource_lifecycle_browser(browser, surface_server, theme, width):
    base,state=surface_server;state.update(reads=0,available=8,failure=False)
    context=browser.new_context(viewport={'width':width,'height':900}, reduced_motion='reduce')
    context.add_cookies([{'name':'surface-owner','value':'7','url':base}])
    page=context.new_page();errors=[];page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(base+'/?runId=owned');page.locator('.rich-workflow').wait_for()
        page.evaluate('(theme)=>{document.documentElement.dataset.theme=theme;document.body.dataset.theme=theme}',theme)
        assert page.locator('.rich-indicator-value').all_text_contents()[2]=='未知'
        assert state['reads']==0  # Hidden resource tabs must not acquire data.
        output=ROOT/'designs/ui-gallery/previews';output.mkdir(exist_ok=True)
        page.screenshot(path=str(output/f'dynamic-overview-{theme}-{width}.png'),full_page=True)
        page.get_by_role('tab',name='资源与队列').click()
        page.locator('.rich-resource-panel[data-state="ready"]').wait_for()
        assert state['reads']==1
        search=page.locator('[data-rich-control="resource-search"]');search.fill('training')
        assert page.locator('.rich-resource-panel tbody tr').count()==1
        page.screenshot(path=str(output/f'dynamic-resources-{theme}-{width}.png'),full_page=True)
        state['available']=0
        page.get_by_role('button',name='刷新',exact=True).click()
        page.wait_for_function("document.querySelector('.rich-resource-panel tbody tr td:nth-child(5)')?.textContent === '0 张'")
        assert search.input_value()=='training'
        state['failure']=True;page.get_by_role('button',name='刷新',exact=True).click()
        page.locator('.rich-resource-panel[data-state="error"]').wait_for()
        assert '上次快照' in page.locator('.rich-resource-status').inner_text()
        assert 'private provider' not in page.locator('body').inner_text()
        assert page.locator('.rich-resource-panel tbody tr td:nth-child(5)').inner_text()=='0 张'
        state['failure']=False;page.get_by_role('button',name='刷新',exact=True).click()
        page.locator('.rich-resource-panel[data-state="ready"]').wait_for()
        page.get_by_role('button',name='并列比较').click()
        assert search.input_value()=='training'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
    finally:context.close()


def test_foreign_scope_does_not_read_provider(browser,surface_server):
    base,state=surface_server;before=state['reads']
    context=browser.new_context();context.add_cookies([{'name':'surface-owner','value':'8','url':base}])
    try:
        response=context.request.get(base+'/api/response-surfaces/resources?runId=owned&provider=demo')
        assert response.status==404 and state['reads']==before
    finally:context.close()


def test_evolution_launcher_requires_explicit_queue_and_releases_on_close(browser,surface_server):
    base,state=surface_server;state.update(reads=0,failure=False)
    context=browser.new_context();context.add_cookies([{'name':'surface-owner','value':'7','url':base}])
    page=context.new_page()
    try:
        page.goto(base+'/?runId=owned');launcher=page.locator('.rich-resource-launcher')
        launcher.locator('summary').click()
        queue=launcher.get_by_label('队列名称');queue.wait_for()
        assert state['reads']==0
        queue.fill('research.training');launcher.get_by_role('button',name='查看资源').click()
        launcher.locator('.rich-resource-panel[data-state="ready"]').wait_for()
        assert state['reads']==1
        launcher.locator(':scope > summary').click()
        page.wait_for_function("!document.querySelector('.rich-resource-launcher .rich-resource-panel')")
        assert launcher.locator('.rich-resource-panel').count()==0
    finally:context.close()
