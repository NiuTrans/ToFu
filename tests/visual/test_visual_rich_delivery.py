"""Real Chromium + isolated Quart production routes/storage + production panel.

Only conversation ownership is a deterministic fixture seam. No gallery dialog,
route body fake, mocked byte fetch, ambient application, or production restart.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import socket
import subprocess
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import zipfile

import pytest
from hypercorn.asyncio import serve
from hypercorn.config import Config
from quart import Quart, Response, g, request

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.visual
pytest_plugins = ('tests._artifact_sidecar',)


@pytest.fixture(scope='module')
def delivery_server(artifact_storage, tmp_path_factory):
    from lib import response_assets
    from lib.artifacts import response_files
    from lib.api_v4 import attach_v4_envelope, copy_blueprint_to_v4
    from routes.api_v1.artifacts import api_v1_artifacts_bp
    from routes.artifacts import artifacts_bp
    from routes.response_assets import response_assets_bp

    directory = tmp_path_factory.mktemp('rich-delivery-http')
    prototype = directory / 'prototype.html'
    # A self-contained prototype with actual JS interaction and >1MiB inline
    # source. The fixture is synthetic, nonsecret and has no external resources.
    source = ('<!doctype html><html><head><style>body{font:16px sans-serif;margin:12px}'
              'button{font:inherit}</style></head><body><h1>Published prototype</h1>'
              '<button id="demo">Demo interaction</button><output id="result">Blocked</output>'
              '<script>window.demoRan=true;document.querySelector("#demo").onclick=()=>{'
              'document.querySelector("#result").textContent="Explicit demo succeeded"};'
              '/*' + 'self-contained prototype fixture ' * 45000 + '*/</script></body></html>')
    prototype.write_text(source, encoding='utf-8')
    binary = directory / 'report.zip'
    with zipfile.ZipFile(binary, 'w') as archive:
        archive.writestr('evidence.bin', b'published immutable attachment\xff\x00\x80')
    markdown = directory / 'README.md'
    markdown.write_text('# Published readme\n\n[unpublished](designs/sidebar-coordinator/preview.html)')
    with patch.object(response_assets, 'get_conversation', lambda conv, user_id, **kw:
                      {'id': conv} if (conv, user_id) == ('delivery-owned', 7) else None):
        documents = [response_files.publish_file(str(path), conv_id='delivery-owned', user_id=7,
                      roots=[str(directory)]) for path in (prototype, binary, markdown)]
        doc_id, file_id, md_id = [item['id'] for item in documents]

        from lib.artifacts.core import create_artifact
        legacy_sources = [source + '\n<!-- legacy inline source -->', '<svg xmlns="http://www.w3.org/2000/svg"><text x="10" y="20">Legacy SVG</text></svg>']
        legacy = [create_artifact(conv_id='delivery-owned', content=text, format=fmt, source=producer,
                   title=name) for text,fmt,producer,name in zip(legacy_sources,
                   ['html','svg'], ['write_file','inline_fence'], ['legacy.html','legacy.svg'])]
        checked_in = ROOT/'designs/sidebar-coordinator/preview.html'
        checked_in_bytes = checked_in.read_bytes()
        checked_in_receipt = response_files.publish_file(str(checked_in), conv_id='delivery-owned', user_id=7,
            roots=[str(checked_in.parent)], title='Checked-in sidebar coordinator prototype')
        entry = directory / 'delivery.ts'
        imports = {
            'createMarkdownRenderer': 'frontend/src/markdown/index.ts',
            'renderRichResponse, disposeRichResponse': 'frontend/src/rich-response/renderer.ts',
            'createButton': 'frontend/src/ui/button.ts',
            'createArtifactsEndpoints': 'frontend/src/api/endpoint-registry/artifacts.ts',
            'createEndpointPorts': 'frontend/src/api/endpoint-registry/ports.ts',
            'apiTransport': 'frontend/src/api/transport.ts',
            'escapeHtml': 'frontend/src/html-safety.ts',
        }
        code = '\n'.join(f'import {{ {names} }} from {json.dumps(str(ROOT/path))};' for names, path in imports.items())
        packages = json.loads(subprocess.check_output(['node','--input-type=module','-e',
            'console.log(JSON.stringify([import.meta.resolve("dompurify"),import.meta.resolve("marked")]))'], cwd=ROOT, text=True))
        code += '\nimport DOMPurify from ' + json.dumps(packages[0].removeprefix('file://')) + ';'
        code += '\nimport { marked } from ' + json.dumps(packages[1].removeprefix('file://')) + ';'
        code += '''
const prefix = location.pathname.startsWith('/proxy/15000/') ? '/proxy/15000' : '';
window.apiUrl = (path) => prefix + path;
window.TOFU_API_V4 = true;
const runtimeScope = {};
const Api = { artifacts: createArtifactsEndpoints(createEndpointPorts(apiTransport, () => 'en')) };
const t = () => '';
const markdown = createMarkdownRenderer({getEnvironment:()=>({document,parser:marked,sanitizer:DOMPurify,
  highlighter:null,mathRenderer:null,basePath:prefix,projectActive:false}),maximumCacheEntries:8,ensureMath:()=>{}});
const renderMarkdown = (text) => markdown.render(text);
'''
        # Compile the exact production adapter in the same lexical module graph.
        # Consume its real production lexical import, not independently supplied
        # helper bindings that could mask a missing shipping-runtime dependency.
        code += (ROOT/'frontend/src/runtime/sections/artifacts.js').read_text().replace(
            "'../rich-response/artifact-delivery'", json.dumps(str(ROOT/'frontend/src/rich-response/artifact-delivery.ts')))
        code += '\nconst ids = ' + json.dumps([doc_id, file_id, md_id] + [row['id'] for row in legacy] + [checked_in_receipt['id']]) + ';\n'
        code += '''
const controls = document.querySelector('#controls');
ids.forEach((id,index)=>{
  const button = createButton({label:['Open prototype','Open attachment','Open readme','Open legacy html','Open legacy svg','Open checked-in prototype'][index],
    onClick:()=>runtimeScope.Artifacts.open(id)});button.id='open-'+index;controls.append(button);
});
document.querySelector('#message').innerHTML = renderMarkdown('[Preview](designs/sidebar-coordinator/preview.html) · [Readme](README.md) · [Route](/settings) · [Web relative](articles/intro.html)');

const bypass = document.querySelector('#bypass');
bypass.innerHTML = renderMarkdown('[Untrusted executable mode]('+prefix+'/api/artifacts/'+ids[0]+'/download?interactive=1) · [Untrusted owned resolver]('+prefix+'/api/response-assets/artifacts/'+ids[0]+'?conversationId=delivery-owned&download=1&%69nteractive=1)');
renderRichResponse(document.querySelector('#rich'),{version:1,title:'Published delivery',blocks:[{
  type:'action_panel',id:'published',title:'Published attachment',actions:[
    {kind:'open_artifact',label:'Open published file',targetId:ids[1]},
    {kind:'download_artifact',label:'Download published file',targetId:ids[1]}
  ]}]},{conversationId:'delivery-owned',openArtifact:id=>runtimeScope.Artifacts.open(id)});
'''
        entry.write_text(code)
        bundle = directory/'delivery.js'
        subprocess.run(['node',str(ROOT/'scripts/vite_test_bundle.mjs'),str(entry),'--bundle',
                        '--format=esm','--platform=browser','--outfile='+str(bundle)], cwd=ROOT, check=True)
        app = Quart(__name__)
        app.register_blueprint(artifacts_bp)
        app.register_blueprint(response_assets_bp)
        app.register_blueprint(attach_v4_envelope(copy_blueprint_to_v4(api_v1_artifacts_bp,name='delivery_artifacts_v4')))
        counts = []
        @app.before_request
        async def identity():
            counts.append(request.path)
            owner = request.cookies.get('delivery-owner')
            if owner:
                g.auth_ctx = SimpleNamespace(is_authenticated=True,owner_user_id=int(owner),key_id='delivery-test',tenant_id=None,scopes=[])
        @app.route('/')
        async def shell():
            prefix = '/proxy/15000' if request.headers.get('X-Delivery-Proxy') else ''
            return ('<!doctype html><html lang="en" data-theme="tofu"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<link rel="stylesheet" href="{prefix}/styles.css"></head><body><main>'
                    '<div id="controls"></div><article id="message" class="md-content"></article><article id="bypass"></article><div id="rich"></div>'
                    f'</main><script type="module" src="{prefix}/delivery.js"></script></body></html>')
        @app.route('/delivery.js')
        async def javascript(): return Response(bundle.read_bytes(), content_type='text/javascript')
        @app.route('/styles.css')
        async def styles(): return Response((ROOT/'static/styles.css').read_bytes(), content_type='text/css')
        # Serve production view's trusted math assets when it requests them.
        @app.route('/static/<path:name>')
        async def static_asset(name):
            candidate = (ROOT/'static'/name).resolve()
            if not candidate.is_relative_to((ROOT/'static').resolve()) or not candidate.is_file(): return '',404
            return await app.send_static_file(name)
        async def proxy(scope,receive,send):
            scope = dict(scope)
            if scope.get('path','').startswith('/proxy/15000/'):
                scope['path']=scope['path'][len('/proxy/15000'):];scope['raw_path']=scope['path'].encode()
                scope['headers']=list(scope.get('headers',[]))+[(b'x-delivery-proxy',b'1')]
            await app(scope,receive,send)
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        config=Config();config.bind=[f'127.0.0.1:{port}'];config.accesslog=None;config.errorlog=None
        stop=Event()
        async def shutdown():
            while not stop.is_set(): await asyncio.sleep(.05)
        thread=Thread(target=lambda:asyncio.run(serve(proxy,config,shutdown_trigger=shutdown)),daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{port}',documents,counts,prototype.read_bytes(),binary.read_bytes(),legacy,[text.encode() for text in legacy_sources],checked_in_receipt,checked_in_bytes
        finally:
            stop.set();thread.join(timeout=5);assert not thread.is_alive()


@pytest.mark.parametrize('width,prefix', [(1180,''),(320,'/proxy/15000')])
def test_published_delivery_real_panel_bytes_consent_reload_and_owner(browser, delivery_server, tmp_path, width, prefix):
    origin,documents,counts,source,binary,_,_,_,_=delivery_server
    doc_id,file_id,md_id=[row['id'] for row in documents]
    page=browser.new_page(viewport={'width':width,'height':1000},accept_downloads=True)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.context.add_cookies([{'name':'delivery-owner','value':'7','url':origin}])
    page.goto(origin+prefix+'/',wait_until='networkidle')
    assert page.locator('#message [role=note]').count()==2
    assert not page.locator('#message a[href*="preview.html"],#message a[href="README.md"]').count()
    assert page.locator('#message a').first.get_attribute('href')=='/settings'
    assert page.locator('#message a').last.get_attribute('href')=='articles/intro.html'
    assert not any('designs/sidebar-coordinator' in path or path.endswith('/README.md') for path in counts)
    # Untrusted model links cannot bypass trusted executable-download confirmation.
    assert page.locator('#bypass a').first.get_attribute('href')==prefix+f'/api/artifacts/{doc_id}/download'
    safe_response=page.request.get(origin+page.locator('#bypass a').first.get_attribute('href'))
    assert safe_response.body()==source and safe_response.headers['content-type'].startswith('text/plain')

    safe_resolver=page.locator('#bypass a').last.get_attribute('href')
    assert 'conversationId=delivery-owned' in safe_resolver and 'download=1' in safe_resolver
    assert 'interactive' not in safe_resolver
    resolved=page.request.get(origin+safe_resolver)
    assert resolved.status==200 and resolved.body()==source
    assert resolved.headers['content-type'].startswith('text/plain')
    # Native published-ID action opens the actual retained panel, not a gallery dummy.
    page.get_by_role('button',name='Open published file',exact=True).focus();page.keyboard.press('Enter')
    panel=page.locator('#artifact-panel');panel.wait_for(state='visible')
    assert panel.locator('[data-art-body]').get_by_role('heading',name='Published attachment').is_visible()
    assert panel.locator('iframe,script').count()==0
    assert file_id not in ''.join(path for path in counts if path.endswith('/raw'))
    assert 'report.zip' in panel.text_content()
    with page.expect_download() as pending:
        panel.get_by_role('button',name='Download original',exact=True).click()
    download=pending.value;assert Path(download.path()).read_bytes()==binary
    assert download.suggested_filename=='report.zip'
    # The existing rich owned resolver downloads decoded original bytes too.
    page.keyboard.press('Escape')
    with page.expect_download() as pending:
        page.get_by_role('button',name='Download published file',exact=True).click()
    assert Path(pending.value.path()).read_bytes()==binary
    page.locator('#open-0').click()
    preview=panel.frame_locator('iframe')
    preview.get_by_role('heading',name='Published prototype').wait_for(state='visible')
    assert len(source)>1024*1024
    response=page.request.get(origin+prefix+documents[0]['rawUrl'])
    assert response.status==200 and response.body()==source
    assert hashlib.sha256(response.body()).hexdigest()==documents[0]['sha256']
    assert panel.locator('iframe').get_attribute('src')==prefix+f'/api/artifacts/{doc_id}/view'
    assert preview.locator('#result').text_content()=='Blocked'
    assert panel.get_by_role('note').text_content().find('network requests')>=0
    with page.expect_download() as pending:
        panel.locator('[data-art-action=download]').click()
    safe=pending.value;assert Path(safe.path()).read_bytes()==source
    assert safe.suggested_filename.endswith('.txt')
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    dialog=page.get_by_role('dialog');assert 'untrusted code' in dialog.text_content()
    page.keyboard.press('Escape');assert not dialog.is_visible()

    # A navigation switch disposes pending consent; it cannot download the next ID.
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    page.evaluate('document.querySelector("#open-1").click()')
    page.get_by_role('dialog').wait_for(state='detached')
    panel.get_by_role('heading',name='Published attachment').wait_for(state='visible')
    page.keyboard.press('Escape');page.locator('#open-0').click()
    preview.get_by_role('heading',name='Published prototype').wait_for(state='visible')
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    with page.expect_download() as pending:
        page.get_by_role('button',name='Download with code / network risk',exact=True).click()
    original=pending.value;assert Path(original.path()).read_bytes()==source
    assert original.suggested_filename=='prototype.html'
    # Demo interaction is exercised ONLY after explicit opt-in; no implied green.
    panel.get_by_role('button',name='Run scripts (code / network risk)',exact=True).focus();page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#artifact-panel iframe")?.hasAttribute("srcdoc")')
    preview.get_by_role('button',name='Demo interaction').wait_for(state='visible')
    preview.get_by_role('button',name='Demo interaction').click()
    assert preview.locator('#result').text_content()=='Explicit demo succeeded'
    assert panel.locator('iframe').get_attribute('sandbox')=='allow-scripts'
    assert page.locator('main #demo,main #result').count()==0
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path/f'rich-delivery-{width}.png'),full_page=True)
    panel.get_by_role('button',name='Close',exact=True).focus();page.keyboard.press('Enter')
    assert panel.locator('iframe').count()==0 # stops the interactive frame, not just hides it
    page.locator('#open-0').click()
    preview.get_by_role('heading',name='Published prototype').wait_for(state='visible')
    assert preview.locator('#result').text_content()=='Blocked' # session consent is gone
    page.reload(wait_until='networkidle');page.locator('#open-0').click()
    preview.get_by_role('heading',name='Published prototype').wait_for(state='visible')
    assert preview.locator('#result').text_content()=='Blocked'
    page.keyboard.press('Escape');assert panel.locator('iframe').count()==0
    page.locator('#open-2').click();assert panel.locator('h1').text_content()=='Published readme'
    assert panel.locator('[role=note]').count()==1
    page.keyboard.press('Escape')
    page.context.clear_cookies();page.locator('#open-0').click()
    panel.get_by_text('Could not load artifact',exact=True).wait_for(state='visible')
    assert panel.locator('iframe').count()==0
    for owner,status in [(None,401),('8',404)]:
        if owner: page.context.add_cookies([{'name':'delivery-owner','value':owner,'url':origin}])
        for url in (f'/api/v4/artifacts/{file_id}',f'/api/artifacts/{file_id}/download',f'/api/artifacts/{doc_id}/raw'):
            assert page.request.get(origin+prefix+url).status==status
    assert not errors,errors
    page.close()


@pytest.mark.parametrize('index', [0, 1])
def test_legacy_download_safe_default_confirmed_original_and_denied_warm_cache(browser, delivery_server, index):
    origin,_,counts,_,_,legacy,source_bytes,_,_=delivery_server
    artifact_id=legacy[index]['id']
    page=browser.new_page(accept_downloads=True)
    page.context.add_cookies([{'name':'delivery-owner','value':'7','url':origin}])
    page.goto(origin+'/',wait_until='networkidle');page.locator(f'#open-{index+3}').click()
    panel=page.locator('#artifact-panel')
    panel.locator('iframe').wait_for(state='visible')
    with page.expect_download() as pending:
        panel.locator('[data-art-action=download]').click()
    safe=pending.value;assert Path(safe.path()).read_bytes()==source_bytes[index]
    assert safe.suggested_filename.endswith('.txt')
    assert any(path==f'/api/artifacts/{artifact_id}/download' for path in counts)
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    page.get_by_role('button',name='Cancel',exact=True).click()
    assert page.get_by_role('dialog').count()==0
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    with page.expect_download() as pending:
        page.get_by_role('button',name='Download with code / network risk',exact=True).click()
    original=pending.value;assert Path(original.path()).read_bytes()==source_bytes[index]
    assert original.suggested_filename==['legacy.html','legacy.svg'][index]
    # Panel content/metadata are warm, yet the explicit download reauthorizes.
    page.context.clear_cookies();before=len(counts)
    with page.expect_response(lambda response:response.url.endswith(f'/api/v4/artifacts/{artifact_id}')) as response:
        panel.locator('[data-art-action=download]').click()
    assert response.value.status==401
    assert f'/api/artifacts/{artifact_id}/download' not in counts[before:]
    page.context.add_cookies([{'name':'delivery-owner','value':'7','url':origin}])
    assert page.request.delete(origin+f'/api/v4/artifacts/{artifact_id}').status==200
    before=len(counts)
    with page.expect_response(lambda response:response.url.endswith(f'/api/v4/artifacts/{artifact_id}')) as response:
        panel.locator('[data-art-action=download]').click()
    assert response.value.status==404
    assert f'/api/artifacts/{artifact_id}/download' not in counts[before:]
    assert page.request.get(origin+f'/api/artifacts/{artifact_id}/download').status==404
    page.close()


def test_checked_in_multimegabyte_prototype_http_bytes_hash_and_safe_panel(browser, delivery_server, tmp_path):
    origin,_,_,_,_,_,_,receipt,source=delivery_server
    assert len(source)>1024*1024
    assert receipt['sizeBytes']==len(source)
    assert receipt['sha256']==hashlib.sha256(source).hexdigest()
    page=browser.new_page(viewport={'width':1180,'height':850},accept_downloads=True)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.context.add_cookies([{'name':'delivery-owner','value':'7','url':origin}])
    page.goto(origin+'/',wait_until='networkidle')
    response=page.request.get(origin+receipt['rawUrl'])
    assert response.status==200 and response.body()==source
    page.locator('#open-5').click()
    panel=page.locator('#artifact-panel')
    iframe=panel.locator('iframe');iframe.wait_for(state='visible')
    page.wait_for_function('document.querySelector("#artifact-panel iframe")?.contentWindow != null')
    assert iframe.get_attribute('src')==f'/api/artifacts/{receipt["id"]}/view'
    assert iframe.get_attribute('srcdoc') is None
    view=page.request.get(origin+iframe.get_attribute('src'))
    assert view.status==200 and "script-src 'self'" in view.headers['content-security-policy']
    assert panel.get_by_role('note').text_content().find('network requests')>=0
    with page.expect_download() as pending:
        panel.locator('[data-art-action=download]').click()
    assert Path(pending.value.path()).read_bytes()==source
    assert pending.value.suggested_filename.endswith('.txt')
    panel.get_by_role('button',name='Download original interactive document',exact=True).click()
    with page.expect_download() as pending:
        page.get_by_role('button',name='Download with code / network risk',exact=True).click()
    assert Path(pending.value.path()).read_bytes()==source
    assert pending.value.suggested_filename=='preview.html'
    # We deliberately do not opt this real document into execution; only the
    # synthetic fixture's known demo interaction above is exercised after consent.
    page.screenshot(path=str(tmp_path/'checked-in-prototype-safe-panel.png'),full_page=True)
    assert not errors,errors
    panel.get_by_role('button',name='Close',exact=True).click()
    assert panel.locator('iframe').count()==0
    page.close()
