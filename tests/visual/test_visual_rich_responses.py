"""Native cards + authenticated image bytes on an isolated real HTTP server.

Only conversation ownership is a fixture lookup. Artifact persistence, image
verification, authentication, byte serving and production renderer are real.
"""
import asyncio
import copy
import json
from pathlib import Path
import socket
import subprocess

import uuid
import time
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from PIL import Image
from quart import Quart, Response, g, request
from hypercorn.asyncio import serve
from hypercorn.config import Config
from lib import response_assets as service
from routes.response_assets import response_assets_bp

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual
pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture(scope='module')
def response_server(artifact_storage, tmp_path_factory):
    from unittest.mock import patch
    directory = tmp_path_factory.mktemp('rich-response-http')
    image = directory / 'chart.png'
    image.write_bytes((ROOT / 'designs/ui-gallery/rich-response-chart.png').read_bytes())
    with Image.open(image) as chart:
        image_width = chart.width
    bundle = directory / 'rich-response.bundle.js'
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'),
                    str(ROOT / 'designs/ui-gallery/rich-response.ts'), '--bundle', '--format=esm',
                    '--platform=browser', '--outfile=' + str(bundle)], cwd=ROOT, check=True)
    demand_entry = directory / 'demand.ts'
    demand_entry.write_text('export {renderRichMarkdown, disposeRichMarkdown} from ' + json.dumps(str(ROOT / 'frontend/src/rich-response/markdown.ts')) + ';export {renderRichResponse,disposeRichResponse} from ' + json.dumps(str(ROOT / 'frontend/src/rich-response/renderer.ts')) + ';')
    demand_bundle = directory / 'demand.js'
    subprocess.run(['node', str(ROOT / 'scripts/vite_test_bundle.mjs'), str(demand_entry), '--bundle', '--format=esm', '--platform=browser', '--outfile=' + str(demand_bundle)], cwd=ROOT, check=True)
    with patch.object(service, 'get_conversation', lambda conv, user_id, **kw:
                      {'id': conv} if (conv, user_id) == ('visual-owned', 7) else None):
        asset = service.publish_asset(str(image), 'Verified chart', conv_id='visual-owned', user_id=7,
                                      roots=[str(directory)])
        import random
        large_image = directory / 'large.png'
        Image.frombytes('RGB', (1500, 1500), random.Random(7).randbytes(1500 * 1500 * 3)).save(large_image)
        assert large_image.stat().st_size > 4 * 1024 * 1024
        large_asset = service.publish_asset(str(large_image), 'Complete large image', conv_id='visual-owned', user_id=7, roots=[str(directory)])
        large_image.unlink()
        payload = json.loads((ROOT / 'designs/ui-gallery/rich-response.fixture.json').read_text())

        from lib.artifacts.core import create_artifact
        report = create_artifact(conv_id='visual-owned', content='Measured performance evidence', format='markdown', source='write_file')
        for block in payload['blocks']:
            if block['type'] == 'image_gallery': block['images'][0]['id'] = asset['id']

            if block['type'] == 'artifact_reference': block['artifactId'] = report['id']
            if block['type'] == 'action_panel':
                for action in block['actions']:
                    if action['kind'].endswith('_artifact'): action['targetId'] = report['id']
            if block['type'] == 'action_panel': block['actions'][0]['targetId'] = asset['id']
        app = Quart(__name__)
        app.register_blueprint(response_assets_bp)
        @app.before_request
        async def identity():
            owner = request.cookies.get('visual-owner')
            if owner: g.auth_ctx = SimpleNamespace(is_authenticated=True, owner_user_id=int(owner),
                                                   key_id='visual-test',tenant_id=None,scopes=[])
        from lib.turn_lifecycle import create_turn_pair, bind_task, record_task_event, get_turn
        from lib.conversation_sync.command_service import ConversationTurnCommandService
        from lib.storage import StorageError
        choice_cases = {}
        choice_service = ConversationTurnCommandService(
            build_user_message=lambda *_: {}, was_aborted_after=lambda *_: False,
            start_task=lambda *_: pytest.fail('live answer must not dispatch another model'),
        )
        def choice_case(case):
            if case not in choice_cases:
                conv = 'browser-choice-' + uuid.uuid4().hex[:12]
                created = create_turn_pair(conv, command_id='initial-' + conv,
                    input_projection={'content': 'Choose implementation scope'}, config={}, user_id=7,
                    conversation_defaults={'allowCreate': True, 'title': 'Browser choice', 'settings': {}})
                attempt = created['attempt']['attemptId']; turn = created['turn']['turnId']
                bind_task(attempt, 'task-' + conv, user_id=7)
                task = {'id': 'task-' + conv, '_attemptId': attempt, '_userId': 7, '_turnId': turn,
                    'convId': conv, 'status': 'running', 'content': '', 'thinking': '', 'config': {},
                    'toolRounds': [{'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-choice',
                        'toolName': 'ask_human', 'toolArgs': {'question': 'Choose scope'}, 'toolContent': None,
                        'status': 'awaiting_human', 'guidanceId': 'hg_browser', 'guidanceQuestion': 'Choose scope',
                        'guidanceType': 'choice', 'guidanceOptions': ['Client', 'Server']}]}
                if case == 'batch':
                    task['toolRounds'][0].update(guidanceType='batch', guidanceOptions=[], guidanceBatchQuestions=[
                        {'question':'Queue', 'response_type':'choice', 'options':[{'label':'Training'},{'label':'Research'}]},
                        {'question':'GPU hours', 'response_type':'free_text', 'options':[]}])
                assert record_task_event(task, {'type': 'tool_result'})
                choice_cases[case] = {'conversationId': conv, 'turnId': turn, 'mode': 'batch' if case == 'batch' else 'choice'}
            return choice_cases[case]
        @app.route('/api/v4/conversations/<conv>/turns/<turn>')
        async def read_choice(conv, turn):
            try:
                return {'ok': True, 'contract': 'tofu.conversation-sync.turn-read/v1',
                    'turn': get_turn(conv, turn, user_id=int(request.cookies.get('visual-owner', '0')))}
            except (StorageError, ValueError) as error:
                return {'message': str(error)}, 404
        @app.route('/api/v4/conversations/<conv>/turns/<turn>/attempts', methods=['POST'])
        async def answer_choice(conv, turn):
            try:
                result = choice_service.create_attempt(conv, turn, int(request.cookies.get('visual-owner', '0')),
                    await request.get_json(), request_started_at=time.time()).value
                return {'ok': True, **result}
            except (StorageError, ValueError) as error:
                return {'message': str(error)}, 409
        @app.route('/')
        async def shell():
            page = (ROOT / 'designs/ui-gallery/rich-response.html').read_text()
            page = page.replace('../../static/styles.css', '/static/styles.css').replace('../../frontend/src/styles/fonts.css', '/fonts.css')
            page = page.replace('./rich-response.bundle.js', '/rich-response.bundle.js')
            # Resolve static shell assets through the same forwarding prefix.
            prefix = '/proxy/15000' if request.headers.get('X-Visual-Proxy') else ''
            for name in ('/static/styles.css', '/fonts.css', '/rich-response.bundle.js'):
                page = page.replace('"' + name + '"', '"' + prefix + name + '"')
            encoded = json.dumps(payload).replace('<', '\\u003c')

            scope = json.dumps(choice_case(request.args.get('choiceCase', 'default'))).replace('<', '\\u003c')
            page = page.replace('<script type="module"', '<script type="application/json" id="choice-scope">' + scope + '</script><script type="module"')
            return page.replace('<script type="module"', '<script type="application/json" id="rich-response-payload">' + encoded + '</script><script type="module"')
        @app.route('/rich-response.bundle.js')
        async def javascript(): return Response(bundle.read_bytes(),content_type='text/javascript')
        @app.route('/large-image-info')
        async def large_info(): return large_asset
        @app.route('/rich-demand.js')
        async def demand_javascript(): return Response(demand_bundle.read_bytes(), content_type='text/javascript')
        @app.route('/static/styles.css')
        async def stylesheet(): return Response((ROOT/'static/styles.css').read_bytes(),content_type='text/css')
        @app.route('/fonts.css')
        async def fonts(): return Response('',content_type='text/css')
        @app.route('/favicon.ico')
        async def favicon(): return '', 204
        counts = []
        @app.before_request
        async def requests():
            if request.path.startswith('/api/response-assets/'): counts.append(request.path)
        async def proxy(scope, receive, send):
            scope = dict(scope)
            if scope.get('path','').startswith('/proxy/15000/'):
                scope['path'] = scope['path'][len('/proxy/15000'):]
                scope['raw_path'] = scope['path'].encode()
                scope['headers'] = list(scope.get('headers',[])) + [(b'x-visual-proxy',b'1')]
            await app(scope,receive,send)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        config = Config(); config.bind=[f'127.0.0.1:{port}'];config.accesslog=None;config.errorlog=None
        stop = Event()
        async def shutdown():
            while not stop.is_set(): await asyncio.sleep(.05)
        thread = Thread(target=lambda:asyncio.run(serve(proxy,config,shutdown_trigger=shutdown)),daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{port}',asset['id'],counts,image_width
        finally:
            stop.set();thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize('theme,width,prefix',[('light',1180,''),('dark',1180,''),('tofu',1180,''),('tofu',320,'/proxy/15000')])
def test_real_asset_rich_cards_keyboard_reload_and_proxy(browser,response_server,tmp_path,theme,width,prefix):
    origin,asset_id,counts,image_width=response_server
    page=browser.new_page(viewport={'width':width,'height':1000})
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))

    expected_denials = [False]
    def console_error(message):
        known_denial = expected_denials[0] and message.text.startswith('Failed to load resource:') \
            and any(path in message.location.get('url', '') for path in ('/api/response-assets/', '/api/v4/conversations/')) \
            and any('status of ' + status in message.text for status in ('401', '403', '404'))
        if message.type == 'error' and not known_denial: errors.append(message.text)
    page.on('console', console_error)
    page.context.add_cookies([{'name':'visual-owner','value':'7','url':origin}])
    page.goto(origin+prefix+'/?choiceCase='+theme+'-'+str(width),wait_until='networkidle')
    page.evaluate('(theme)=>{document.documentElement.dataset.theme=theme;document.body.dataset.theme=theme;}',theme)
    image=page.locator('#rich-preview img')
    image.wait_for(state='visible')
    assert image.evaluate('(img,width)=>img.complete && img.naturalWidth===width',image_width)
    assert page.locator('#rich-preview .rich-block').count()==7
    timeline = page.locator('#rich-inline-preview [data-rich-block="capture-timing"]')
    assert timeline.locator('svg').count() == 1
    assert timeline.locator('svg').is_visible()
    assert timeline.locator('table').count() == 1
    assert 'Shutter' in timeline.text_content()
    assert 'Ring buffer' in timeline.text_content()
    assert timeline.locator('svg rect').count() >= 3
    svg_bounds = timeline.locator('svg').bounding_box()
    assert svg_bounds['width'] > 200 and svg_bounds['height'] > 100
    assert timeline.locator('script,img,[onload],[onclick]').count() == 0
    assert page.locator('#rich-inline-preview').get_attribute('data-terminal') == 'true'
    assert page.locator('.rich-metric[data-status=failed]').count()==1
    assert page.locator('.rich-metric').count()==4
    assert page.locator('.rich-warning').count()==2
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    assert '/api/response-assets/'+asset_id in counts
    details=page.locator('[data-rich-block=evidence]')
    details.locator('summary').focus();page.keyboard.press('Enter');assert not details.evaluate('(n)=>n.open')
    page.keyboard.press('Enter');assert details.evaluate('(n)=>n.open')
    page.locator('[data-rich-block=actions] button').nth(1).click()
    page.locator('dialog[open]').wait_for(state='visible')
    page.keyboard.press('Escape');assert page.locator('dialog[open]').count()==0
    components = page.locator('#rich-components-preview')
    assert components.locator('.rich-data-chart-plot').count() == 4
    assert components.locator('.rich-data-chart-table table').count() == 4
    assert components.locator('[data-rich-block="scatter-chart"] svg').get_attribute('data-y-domain') == '-2,3'
    assert components.locator('[data-rich-block="pie-chart"] svg path').count() == 3

    assert components.locator('[data-rich-block="pie-chart"] svg').bounding_box()['width'] <= components.bounding_box()['width']
    source = components.locator('.rich-citation-inline button')
    source.focus(); page.keyboard.press('Enter')
    preview = page.locator('dialog[open]')
    assert 'Supplied excerpt' in preview.text_content()
    assert preview.locator('a').get_attribute('rel') == 'noopener noreferrer'
    assert preview.locator('a').get_attribute('href') == 'https://fixture.example/guide'
    page.keyboard.press('Tab')
    assert preview.evaluate('(node) => node.contains(document.activeElement)')
    page.keyboard.press('Escape')
    assert page.locator('dialog[open]').count() == 0
    assert source.evaluate('(node) => node === document.activeElement')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    choices = page.locator('#rich-choices-preview')
    choices.locator('input[type="radio"]').first.wait_for(state='visible')
    submit = choices.get_by_role('button', name='Submit answer')
    assert submit.is_disabled()
    radio = choices.locator('input[type="radio"]').first
    radio.focus(); page.keyboard.press('ArrowDown')
    assert choices.locator('input[value="Server"]').is_checked()
    assert submit.is_enabled()
    with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/attempts')) as accepted:
        submit.click()
    reply = accepted.value
    assert reply.status == 200, reply.text()
    payload = reply.json()

    (tmp_path / 'choice-reply.json').write_text(json.dumps(payload, indent=2))
    assert payload['turn']['projection']['toolRounds'][0]['guidanceResponse'] == 'Server', payload
    page.wait_for_function("document.querySelector('#rich-choices-preview [role=status]').textContent.includes('saved') || document.querySelector('#rich-choices-preview [role=status]').textContent.includes('rejected')")
    assert 'Answer saved: Server' in choices.text_content(), {'ui': choices.text_content(), 'errors': errors, 'replyPath': str(tmp_path / 'choice-reply.json')}
    choices.get_by_text('Answer saved: Server').wait_for(state='visible')
    assert choices.locator('fieldset').evaluate('(node) => node.disabled')
    assert choices.locator('input[type="radio"]').first.is_disabled()
    assert submit.is_disabled()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
    preview_dir = ROOT / 'designs/ui-gallery/previews'
    preview_dir.mkdir(exist_ok=True)
    page.evaluate("() => { window.scrollTo(0, 0); for (const node of document.querySelectorAll('*')) { if (node.scrollTop) node.scrollTop = 0; if (node.scrollLeft) node.scrollLeft = 0; } }")
    path=preview_dir/f'rich-components-{theme}-{width}.png';page.screenshot(path=str(path),full_page=True)
    page.reload(wait_until='domcontentloaded');page.locator('#rich-preview img').wait_for(state='visible')
    assert page.locator('.rich-metric').count()==4
    assert page.locator('#rich-inline-preview .rich-timeline-plot').is_visible()

    assert page.locator('#rich-components-preview .rich-data-chart-plot').count() == 4
    assert page.locator('#rich-components-preview .rich-citation-inline button').is_enabled()

    page.locator('#rich-choices-preview').get_by_text('Answer saved: Server').wait_for(state='visible')
    assert page.locator('#rich-choices-preview input[value="Server"]').is_checked()
    assert not errors, errors
    expected_denials[0] = True
    page.context.clear_cookies();page.reload(wait_until='networkidle')
    page.locator('[data-rich-control="retry:'+asset_id+'"]').wait_for(state='visible')
    assert not page.locator('#rich-preview img').count()
    page.context.add_cookies([{'name':'visual-owner','value':'8','url':origin}])
    page.locator('[data-rich-control="retry:'+asset_id+'"]').click()
    page.wait_for_timeout(100)
    assert page.locator('[data-rich-control="retry:'+asset_id+'"]').is_visible()
    assert not errors,errors
    page.close()


def test_complete_large_rich_message_and_all_fences(browser, response_server):
    origin, _, _, _ = response_server
    page = browser.new_page(viewport={'width': 1180, 'height': 1000})
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(origin)
        result = page.evaluate("""async () => {
          const {renderRichMarkdown,disposeRichMarkdown}=await import('/rich-demand.js');
          const table={version:1,title:'Complete',blocks:[{type:'table',id:'complete',title:'Complete table',columns:Array(12).fill('Column'),rows:Array.from({length:100},(_,r)=>Array.from({length:12},(_,c)=>`${r}:${c}:`+'x'.repeat(980)))}]};
          const small={version:1,title:'Later fence',blocks:[{type:'table',id:'later',title:'Later',columns:['Column'],rows:[['Ninth fence']]}]};
          const fence=p=>'```tofu-ui\\n'+JSON.stringify(p)+'\\n```';
          const source=[fence(table),...Array(8).fill(fence(small))].join('\\n');
          const host=document.createElement('section');document.body.replaceChildren(host);
          renderRichMarkdown(host,source,true,{renderSafeMarkdownHtml:s=>s});
          const result={bytes:new TextEncoder().encode(source).length,cards:host.querySelectorAll('.rich-response').length,rows:host.querySelectorAll('tbody tr').length,tail:host.textContent.includes('99:11:'),ninth:host.textContent.includes('Ninth fence')};
          disposeRichMarkdown(host);host.remove();return result;
        }""")
        assert result['bytes'] > 1024 * 1024
        assert result['cards'] == 9 and result['rows'] == 108
        assert result['tail'] and result['ninth']
        assert errors == []
    finally:
        page.close()


def test_native_batch_form_persists_through_real_turn_command_and_reload(browser, response_server):
    origin, *_ = response_server
    context = browser.new_context(viewport={'width': 720, 'height': 900})
    context.add_cookies([{'name':'visual-owner','value':'7','url':origin}])
    page = context.new_page()
    try:
        page.goto(origin + '/?choiceCase=batch')
        form = page.locator('#rich-choices-preview .rich-guidance-form')
        form.locator('select').wait_for(state='visible')
        submit = form.get_by_role('button', name='Submit answer')
        assert submit.is_disabled()
        form.locator('select').select_option('Training')
        form.locator('input').fill('24')
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/attempts')) as saved:
            submit.click()
        assert saved.value.status == 200, saved.value.text()
        turn = saved.value.json()['turn']
        response = turn['projection']['toolRounds'][0]['guidanceResponse']
        assert json.loads(response) == [{'index':0,'answer':'Training'},{'index':1,'answer':'24'}]
        form.get_by_text('Answer saved', exact=True).wait_for()
        page.reload()
        form.get_by_text('Answer saved', exact=True).wait_for()
        assert form.locator('input').input_value() == '24'
        assert form.locator('select').input_value() == 'Training'
        assert submit.is_disabled()
    finally:
        context.close()


def test_complete_large_published_image_in_browser(browser, response_server):
    origin, _, _, _ = response_server
    page = browser.new_page(viewport={'width': 1180, 'height': 1000})
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.context.add_cookies([{'name': 'visual-owner', 'value': '7', 'url': origin}])
        page.goto(origin)
        size = page.evaluate("""async () => {
          const {renderRichResponse}=await import('/rich-demand.js');
          const asset=await (await fetch('/large-image-info')).json();
          const host=document.createElement('section');host.id='large-image-check';document.body.replaceChildren(host);
          renderRichResponse(host,{version:1,title:'Complete',blocks:[{type:'image_gallery',id:'large',title:'Large',images:[{id:asset.id,alt:asset.alt}]}]});
          return asset.sizeBytes;
        }""")
        assert size > 4 * 1024 * 1024
        page.wait_for_function("() => { const img=document.querySelector('#large-image-check img');return img && img.complete && img.naturalWidth===1500 && !img.hidden;}")
        assert page.locator('#large-image-check img').evaluate('(img)=>img.naturalHeight') == 1500
        page.evaluate("""async () => {const {disposeRichResponse}=await import('/rich-demand.js');const host=document.getElementById('large-image-check');disposeRichResponse(host);host.remove();}""")
        assert errors == []
    finally:
        page.close()


def test_complete_rich_collections_and_late_citations(browser, response_server):
    origin, _, _, _ = response_server
    page = browser.new_page(viewport={'width': 1180, 'height': 1000})
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(origin)
        result = page.evaluate("""async () => {
          const {renderRichMarkdown,disposeRichMarkdown}=await import('/rich-demand.js');
          const table={type:'table',id:'table',title:'Complete',columns:Array(13).fill('Column'),rows:Array.from({length:101},(_,r)=>Array.from({length:13},(_,c)=>`${r}:${c}`))};
          const blocks=Array.from({length:25},(_,i)=>({...table,id:'table'+i}));
          blocks.push(...Array.from({length:25},(_,i)=>({type:'citations',id:'sources'+i,title:'Sources '+i,sources:Array.from({length:33},(_,s)=>({id:'item'+s,title:'Source '+s,url:'https://example.com/'+s}))})));
          const payload={version:1,title:'Complete',blocks};
          const source='Last source [[cite:sources24/item32]]\\n```tofu-ui\\n'+JSON.stringify(payload)+'\\n```';
          const host=document.createElement('section');document.body.replaceChildren(host);
          renderRichMarkdown(host,source,true,{renderSafeMarkdownHtml:s=>s});
          const chip=host.querySelector('.rich-citation-inline button');
          const result={blocks:host.querySelectorAll('.rich-block').length,rows:host.querySelectorAll('tbody tr').length,tail:host.querySelector('[data-rich-block="table24"]').textContent.includes('100:12'),source:chip && !chip.disabled};
          disposeRichMarkdown(host);host.remove();return result;
        }""")
        assert result == {'blocks': 50, 'rows': 2525, 'tail': True, 'source': True}
        assert errors == []
    finally:
        page.close()
