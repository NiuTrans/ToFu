"""Edge contracts for rich reconciliation; browser checks use a local HTTP fixture."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from tests.frontend.test_round_card_stability import (
    _run,
    round_card_stability_bundle as round_card_stability_bundle,
)


@pytest.mark.unit
def test_rich_card_explicit_keys_do_not_retarget_controls(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
for (const [before,after] of [
  ['<button id="same" data-tofu-action="approve(this)">Approve</button>',
   '<button id="same" data-tofu-action="deny(this)">Deny</button>'],
  ['<button data-rich-key="same" data-conversation-action="retry">Retry</button>',
   '<button data-rich-key="same" data-conversation-action="cancel">Cancel</button>'],
  ['<textarea id="same" data-tofu-action-keydown="approve(this)"></textarea>',
   '<textarea id="same" data-tofu-action-keydown="deny(this)"></textarea>'],
  ['<input id="same" name="first" value="first">',
   '<input id="same" name="second" value="second">'],
  ['<a data-rich-key="same" href="/first">First</a>',
   '<a data-rich-key="same" href="/second">Second</a>'],
  ['<button id="same" data-label="old" data-tofu-action="selectBatchChoice(this)">Old</button>',
   '<button id="same" data-label="new" data-tofu-action="selectBatchChoice(this)">New</button>'],
]) {
  root.replaceChildren(); patch(root,before);
  const old=root.firstChild; old.focus();
  patch(root,after);
  assert.notEqual(root.firstChild,old); assert.equal(old.isConnected,false);
  assert.notEqual(document.activeElement,old);
}
// Attribute order alone is not an operation change.
patch(root,'<button id="same" data-tofu-action="approve(this)" data-tofu-action-keydown="key(event)">A</button>');
const button=root.firstChild; button.focus();
patch(root,'<button data-tofu-action-keydown="key(event)" data-tofu-action="approve(this)" id="same">Updated</button>');
assert.equal(root.firstChild,button); assert.equal(document.activeElement,button);
assert.equal(button.textContent,'Updated');
""")


@pytest.mark.unit
def test_rich_card_attributes_and_removed_children_remain_authoritative(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
patch(root,'<div class="ptool-panel active"><button aria-expanded="true">Live</button></div>');
const panel=root.firstChild,header=panel.firstChild;
panel.setAttribute('data-stale-loaded','yes'); panel.setAttribute('onclick','stale()');
panel.classList.add('stale-runtime');
patch(root,'<div class="ptool-panel collapsed" data-collapsible="true"><button aria-expanded="false">Settled</button></div>');
assert.equal(root.firstChild,panel);
assert.ok(panel.classList.contains('collapsed')); // A new fold uses presenter defaults.
assert.equal(panel.hasAttribute('data-stale-loaded'),false);
assert.equal(panel.hasAttribute('onclick'),false);
assert.equal(panel.classList.contains('stale-runtime'),false);
panel.classList.remove('collapsed'); header.setAttribute('aria-expanded','true');
patch(root,'<div class="ptool-panel collapsed failed" data-collapsible="true"><button aria-expanded="false" disabled title="updated">Error</button></div>');
assert.equal(root.firstChild,panel); assert.equal(panel.firstChild,header);
assert.equal(panel.classList.contains('collapsed'),false);
assert.equal(header.getAttribute('aria-expanded'),'true');
assert.equal(header.disabled,true); assert.equal(header.title,'updated');
assert.equal(header.textContent,'Error');
patch(root,'<div class="ptool-panel"><button>Not collapsible</button></div>');
assert.equal(panel.hasAttribute('data-collapsible'),false);
assert.equal(panel.classList.contains('collapsed'),false);
assert.equal(header.hasAttribute('aria-expanded'),false); assert.equal(header.disabled,false);
const form=label=>`<section><input name="draft" value="default"><input name="check" type="checkbox"><textarea name="note">default</textarea><select name="choice"><option value="a">A</option><option value="b">B</option></select><p>${label}</p></section>`;
patch(root,form('one'));
const draft=root.querySelector('[name=draft]'),check=root.querySelector('[name=check]'),note=root.querySelector('textarea'),select=root.querySelector('select');
draft.value='reader'; check.checked=true; note.value='reader note'; select.value='b';
patch(root,form('two'));
assert.equal(draft.value,'reader'); assert.equal(check.checked,true);
assert.equal(note.value,'reader note'); assert.equal(select.value,'b');
// Removed fields/output must not be resurrected; re-added IDs start fresh.
const section=root.firstChild;
const extra=document.createElement('aside'); extra.textContent='stale runtime output'; section.append(extra);
patch(root,'<section><p>redacted</p></section>');
assert.equal(root.firstChild,section); assert.equal(section.textContent,'redacted');
for(const node of [draft,check,note,select,extra]) assert.equal(node.isConnected,false);
patch(root,form('three')); assert.notEqual(root.querySelector('[name=draft]'),draft);
assert.equal(root.querySelector('[name=draft]').value,'default');
""")


@pytest.mark.unit
def test_rich_card_swarm_thinking_and_runtime_state_lifetimes(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const agent=(status,open='')=>`<div class="sw-agent ${open}" data-agent-id="a" data-sw-agent-status="${status}"><p>${status}</p></div>`;
patch(root,agent('pending')); const node=root.firstChild;
patch(root,agent('thinking','sw-a-open')); assert.equal(root.firstChild,node);
assert.equal(node.classList.contains('sw-a-open'),true);
node.classList.remove('sw-a-open'); node.setAttribute('data-sw-reader-open','false');
patch(root,agent('running','sw-a-open')); assert.equal(node.classList.contains('sw-a-open'),false);
patch(root,agent('completed')); assert.equal(node.textContent,'completed');
assert.equal(node.getAttribute('data-sw-reader-open'),'false');
patch(root,'<div data-agent-id="a" class="not-an-agent">Different kind</div>');
assert.equal(root.firstChild,node); assert.equal(node.hasAttribute('data-sw-reader-open'),false);
assert.equal(node.classList.contains('sw-a-open'),false);
""")


@pytest.mark.unit
def test_rich_card_does_not_restore_selection_over_changed_middle(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const card=middle=>`<section data-rich-key="first"><p>begin</p><p>${middle}</p><p>end</p></section>`;
const other='<section data-rich-key="second">other</section>';
patch(root,card('middle')+other);
const first=root.firstChild,last=root.lastChild;
const start=first.children[0].firstChild,end=first.children[2].firstChild;
const selection=document.getSelection(); selection.setBaseAndExtent(end,2,start,1);
const set=selection.setBaseAndExtent.bind(selection); let restored=0;
selection.setBaseAndExtent=(...args)=>{restored++;return set(...args);};
patch(root,other+card('revised'));
assert.equal(root.firstChild,last); assert.equal(root.lastChild,first);
assert.equal(restored,0); assert.equal(first.children[1].textContent,'revised');
""")


@pytest.mark.unit
def test_rich_card_presenter_safety_survives_incremental_updates(round_card_stability_bundle):
    _run(round_card_stability_bundle, r"""
const presenter=feature.createToolCommandExecutionPresentation({translate:key=>key});
const header={iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''};
const round={toolName:'run_command',roundNum:1,toolCallId:'command',query:'printf safe'};
const html=output=>presenter.renderSettledCommandHtml(round,{exitCode:0,output},header,{bodyExpanded:false,outputExpanded:true});
patch(root,html('first output')); const card=root.firstChild;
const pre=card.querySelector('.ptool-cmd-output');
const attack='<img src=x onerror="globalThis.pwned=1"><script>globalThis.pwned=1</script>';
patch(root,html(attack));
assert.equal(root.firstChild,card); assert.equal(card.querySelector('.ptool-cmd-output'),pre);
assert.ok(pre.textContent.includes(attack)); assert.equal(root.querySelector('script,img,iframe'),null);
assert.equal(root.querySelector('[onerror],[onclick]'),null); assert.equal(dom.window.pwned,undefined);
const escapeText=value=>String(value).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const guidance=feature.createToolHumanGuidancePresentation({translate:key=>key,renderMarkdown:escapeText});
const question={status:'awaiting_human',toolName:'ask_human',guidanceId:'q',guidanceType:'choice',guidanceQuestion:'Question',guidanceOptions:[{label:'safe'}]};
patch(root,guidance.renderGuidanceHtml(question,{iconHtml:''}));
const oldButton=root.querySelector('button'); oldButton.focus();
patch(root,guidance.renderGuidanceHtml({...question,guidanceQuestion:attack,guidanceOptions:[{label:attack}]},{iconHtml:''}));
assert.equal(root.querySelector('script,img,[onerror]'),null);
const newButton=root.querySelector('button'); assert.notEqual(newButton,oldButton);
assert.equal(newButton.dataset.label,attack);
assert.equal(newButton.getAttribute('data-tofu-action'),'event.stopPropagation();submitHumanGuidanceChoice(this)');
assert.equal(dom.window.pwned,undefined);
""")


@pytest.fixture
def rich_card_http_server(round_card_stability_bundle):
    bundle = round_card_stability_bundle.read_bytes()
    image_requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/bundle.js':
                body, mime = bundle, 'text/javascript'
            elif self.path.startswith('/image.svg'):
                image_requests.append(self.path)
                body = b'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8"><rect width="8" height="8" fill="green"/></svg>'
                mime = 'image/svg+xml'
            else:
                body = b'<!doctype html><html><head><script src="/bundle.js"></script></head><body><main id="root"></main></body></html>'
                mime = 'text/html'
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', image_requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.visual
@pytest.mark.parametrize('fallback_move', [False, True])
def test_rich_card_browser_reader_state_and_images(browser, rich_card_http_server, fallback_move):
    origin, image_requests = rich_card_http_server
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(origin)
        page.evaluate(r"""(fallback) => {
          window.patch=roundCardStability.reconcileRichCardHtml;
          window.root=document.getElementById('root');
          if(fallback) Object.defineProperty(root,'moveBefore',{value:undefined});
          window.card=(rev)=>`<details data-rich-key="card" open><summary tabindex="0">Output</summary><p class="selection">unchanged reader selection</p><input name="draft" value="server"><textarea name="note">server</textarea><select name="choice"><option value="a">A</option><option value="b">B</option></select><img src="/image.svg" width="8" height="8"><pre style="height:50px;width:150px;overflow:auto">revision ${rev}\n${'line with very wide output '.repeat(20)+'\n'.repeat(2)+('line\n'.repeat(100))}</pre><button class="action" data-conversation-action="inspect">Inspect</button></details>`;
          window.other='<aside data-rich-key="other">Other</aside>';
          patch(root,card(0)+other);
          window.details=root.firstChild;
          window.summary=details.querySelector('summary');
          window.image=details.querySelector('img');
          window.scrollBox=details.querySelector('pre');
          window.draft=details.querySelector('input');
          window.note=details.querySelector('textarea');
          window.choice=details.querySelector('select');
          window.button=details.querySelector('button');
          window.text=details.querySelector('.selection').firstChild;
          window.loads=0; image.addEventListener('load',()=>loads++);
          window.clicks=0; button.addEventListener('click',()=>clicks++);
        }""", fallback_move)
        page.wait_for_function('image.complete && image.naturalWidth === 8')
        result = page.evaluate(r"""() => {
          const check=(condition,message)=>{if(!condition) throw new Error(message);};
          const initialLoads=loads;
          draft.value='reader draft'; note.value='reader note'; choice.value='b';
          summary.focus(); scrollBox.scrollTop=220; scrollBox.scrollLeft=90;
          const top=scrollBox.scrollTop,left=scrollBox.scrollLeft;
          const selection=document.getSelection(); selection.setBaseAndExtent(text,4,text,19);
          for(let i=1;i<=20;i++) {
            patch(root,i%2?other+card(i):card(i)+other);
            check(root.querySelector('details')===details,'details identity');
            check(root.querySelector('summary')===summary,'summary identity');
            check(document.activeElement===summary,'summary focus');
            check(details.open,'native disclosure');
            check(selection.anchorNode===text && selection.anchorOffset===4 && selection.focusOffset===19,'selection');
            check(scrollBox.scrollTop===top && scrollBox.scrollLeft===left,'scroll');
            check(scrollBox.textContent.startsWith('revision '+i+'\n'),'updated content');
            check(root.querySelector('img')===image && image.complete,'loaded image identity');
          }
          draft.focus(); draft.setSelectionRange(2,8,'backward');
          patch(root,other+card(21));
          check(document.activeElement===draft && draft.value==='reader draft','input focus and value');
          check(draft.selectionStart===2 && draft.selectionEnd===8 && draft.selectionDirection==='backward','input selection');
          check(note.value==='reader note' && choice.value==='b','textarea and select values');
          button.click(); check(clicks===1,'listener retained once');
          details.open=false; patch(root,card(22)+other); check(!details.open,'reader closed stays closed');
          return {initialLoads,loads,top,left};
        }""")
        assert result['top'] > 0 and result['left'] > 0
        assert result['loads'] == result['initialLoads']
        assert image_requests == ['/image.svg']
        # A real source change must update the same image, not freeze old output.
        page.evaluate("patch(root,card(23).replace('/image.svg','/image.svg?v=2')+other)")
        page.wait_for_function("image.complete && image.currentSrc.endsWith('?v=2')")
        assert page.evaluate("root.querySelector('img')===image")
        assert image_requests == ['/image.svg', '/image.svg?v=2']
        page.evaluate("patch(root,'<p>output removed</p>')")
        assert page.evaluate('!details.isConnected && !image.isConnected && root.textContent === "output removed"')
        assert not errors
    finally:
        page.close()
