"""Real renderer stream/terminal sanitation under an owned strict-CSP origin.

Loads actual renderer owners and installed Marked/DOMPurify through permitted
same-origin scripts. This is browser security/DOM parity, not main-app CSP,
backend integration, Trusted Types or performance admission.
"""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
import pytest
from playwright.sync_api import expect
from tests._runtime_sections import ROOT,native_module_path

pytestmark=pytest.mark.visual
CSP="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-src 'none'; form-action 'none'"

RUNNER=r"""(() => {
  installMarkdownPolicy(marked);
  const renderer=createMarkdownRenderer({maximumCacheEntries:300,
    getEnvironment:()=>({document,parser:marked,sanitizer:DOMPurify,highlighter:null,
      mathRenderer:null,basePath:'',projectActive:false}),ensureMath(){}});
  const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
  const streamed=document.querySelector('#streamed'),oracle=document.querySelector('#oracle');
  const status=document.querySelector('#status'),violations=[];
  document.addEventListener('securitypolicyviolation',event=>{violations.push(event.effectiveDirective);
    if(event.effectiveDirective.startsWith('script-src')) document.querySelector('#csp-proof').textContent=window.__unsafeControlExecuted?'unsafe':'blocked';});
  let count=0,completed=0;
  const dangerous=node=>Array.from(node.querySelectorAll('*')).flatMap(element=>
    Array.from(element.attributes).filter(attribute=>/^on/i.test(attribute.name)
      || (['href','src','xlink:href','action','formaction'].includes(attribute.name.toLowerCase())
          && /^(javascript:|vbscript:|data:text\/html)/i.test(attribute.value.replace(/[\x00-\x20]/g,'')))));
  const proof=window.__streamSecurity={violations,done:false,frames:0,streams:0,error:null,
    close(){renderer.dispose();streamed.replaceChildren();oracle.replaceChildren();}};
  document.querySelector('#run').addEventListener('click',async()=>{
    try {
      const cases=await (await fetch('/cases.json')).json();
      const prefix='Stable 中文 😀 **reader prefix**.\n\nSecond paragraph.\n\n';
      for(const payload of cases) for(const context of ['', '```html\n', '| A | B |\n|---|---|\n| safe | value |\n\n']) {
        const id='security-'+completed;
        renderTextBlock(streamed,{kind:'text',blockId:id,markdown:prefix,terminal:false},ports);
        const body=streamed.querySelector('.md-content'),first=body.firstElementChild;
        const original=first.outerHTML;
        const input=context+payload;
        const cuts=[...new Set([0,1,2,3,8,16,32,Math.floor(input.length/2),input.length-1,input.length])]
          .filter(value=>value>=0 && value<=input.length).sort((a,b)=>a-b);
        for(const cut of cuts) {
          const text=prefix+input.slice(0,cut);
          renderTextBlock(streamed,{kind:'text',blockId:id,markdown:text,terminal:false},ports);
          oracle.innerHTML=renderer.render(text,{streaming:true});
          if(body.innerHTML!==oracle.innerHTML) throw new Error('stream DOM mismatch '+completed+' cut '+cut);
          if(dangerous(body).length || body.querySelector('script,iframe,object,embed')) throw new Error('unsafe streamed DOM '+completed);
          if(oracle.firstElementChild?.outerHTML===original && body.firstElementChild!==first) throw new Error('stable prefix identity lost '+completed);
          count++;
          await new Promise(resolve=>requestAnimationFrame(resolve));
        }
        const text=prefix+input;
        renderTextBlock(streamed,{kind:'text',blockId:id,markdown:text,terminal:true},ports);
        oracle.innerHTML=renderer.render(text);
        if(body.innerHTML!==oracle.innerHTML || dangerous(body).length || body.querySelector('script,iframe,object,embed')) throw new Error('unsafe/mismatched terminal DOM '+completed);
        completed++;count++;
      }
      proof.frames=count;proof.streams=completed;proof.done=true;status.textContent='passed';
    } catch(error) {proof.error=String(error);proof.done=true;status.textContent='failed';}
    finally {document.querySelector('#report').textContent=JSON.stringify({frames:proof.frames,streams:proof.streams,error:proof.error,violations,attack:Boolean(window.__attack)});proof.close();}
  });
})();"""

PAYLOADS=[
 '<script>window.__attack=true</script>',
 '<img src="/missing" onerror="window.__attack=true">',
 '<svg onload="window.__attack=true"></svg>',
 '<svg><a xlink:href="javascript:window.__attack=true">click</a></svg>',
 '<a href="java&#x0a;script:window.__attack=true">click</a>',
 '[click](javascript:window.__attack=true)',
 '[click](data:text/html,<script>window.__attack=true</script>)',
 '<iframe srcdoc="<script>window.__attack=true</script>"></iframe>',
 '<object data="javascript:window.__attack=true"></object>',
 '<form action="javascript:window.__attack=true"><input name="location"></form>',
 '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=/missing onerror=window.__attack=true>">',
 '<svg><foreignObject><p onmouseover="window.__attack=true">x</p></foreignObject></svg>',
 '<table><tr><td><svg><desc><img src=/missing onerror=window.__attack=true>',
 '<noscript><p title="</noscript><img src=/missing onerror=window.__attack=true>">',
 '<template><img src=/missing onerror=window.__attack=true></template>',
 '<a href="vbscript:window.__attack=true">click</a>',
 '<a href=" JAVASCRIPT:window.__attack=true">click</a>',
 '<img src="data:text/html,<script>window.__attack=true</script>">',
 '<math href="javascript:window.__attack=true"><mtext>x</mtext></math>',
 '<select><option><style></select><img src=/missing onerror=window.__attack=true>',
 '**benign emphasis** and 中文 😀',
 '<p><b>nested safe text</b></p>',
 '| Name | Value |\n|---|---|\n| item | 1 |',
 '[source][ref]\n\n[ref]: /safe-source',
 '```js\nconst value = "<img onerror=attack>";\n```',
 '&lt;script&gt;literal&lt;/script&gt;',
 '<svg><circle cx="1" cy="1" r="1"></circle></svg>',
 '<math><mi>x</mi><mo>+</mo><mn>1</mn></math>',
]

@pytest.fixture(scope='module')
def streaming_security_origin():
    assets={'/marked.js':(ROOT/'node_modules/marked/lib/marked.umd.js').read_bytes(),
      '/purify.js':(ROOT/'node_modules/dompurify/dist/purify.min.js').read_bytes(),
      '/runner.js':RUNNER.encode(),'/cases.json':json.dumps(PAYLOADS).encode()}
    owners={'/markdown.js':'frontend/src/markdown/index.ts','/policy.js':'frontend/src/markdown-policy.ts',
      '/text.js':'frontend/src/conversation/ui/renderers/text-blocks.ts'}
    for uri,source in owners.items():assets[uri]=Path(native_module_path('.native/security-'+Path(uri).stem+'.js',ROOT/source)).read_bytes()
    markup=('<!doctype html><meta charset="utf-8"><button id="blocked" onclick="window.__unsafeControlExecuted=true">CSP control</button>'
      '<button id="run">Run stream corpus</button><p id="status">ready</p><p id="csp-proof">pending</p><pre id="report"></pre><section id="streamed"></section><div id="oracle"></div>'
      +''.join('<script src="'+uri+'"></script>' for uri in ('/marked.js','/purify.js','/markdown.js','/policy.js','/text.js','/runner.js'))).encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            body=markup if self.path=='/' else assets.get(self.path)
            if body is None:self.send_error(404);return
            self.send_response(200);self.send_header('Content-Security-Policy',CSP)
            self.send_header('Content-Type','text/html; charset=utf-8' if self.path=='/' else 'application/json' if self.path.endswith('.json') else 'application/javascript')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',{uri:hashlib.sha256(body).hexdigest() for uri,body in assets.items()}
    finally:server.shutdown();server.server_close();thread.join(timeout=5);assert not thread.is_alive()


def test_streaming_and_terminal_sanitation_under_strict_csp(browser,streaming_security_origin):
    origin,assets=streaming_security_origin;page=browser.new_page();errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        response=page.goto(origin,wait_until='load');assert response.headers['content-security-policy']==CSP
        page.locator('#blocked').click();expect(page.locator('#csp-proof')).to_have_text('blocked')
        page.locator('#run').click();expect(page.locator('#status')).not_to_have_text('ready',timeout=120000)
        expect(page.locator('#report')).not_to_have_text('')
        result=json.loads(page.locator('#report').text_content())
        assert result['error'] is None and not result['attack'] and not errors,(result,errors)
        assert result['streams']==len(PAYLOADS)*3 and result['frames']>=result['streams']*6
        destination=os.environ.get('TOFU_STREAMING_SECURITY_OUTPUT')
        if destination:
            sources=('tests/visual/test_visual_streaming_markdown_security.py','frontend/src/conversation/ui/renderers/text-blocks.ts',
              'frontend/src/conversation/ui/renderers/stable-dom.ts','frontend/src/markdown/index.ts','frontend/src/markdown-policy.ts')
            Path(destination).write_text(json.dumps({'scope':__doc__,'whole_system_verdict':'incomplete','browser':browser.version,
              'csp':CSP,'checks':result,'assets':assets,'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}},indent=2)+'\n')
    finally:page.close()
