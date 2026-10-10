"""Compiled article outcomes: safe DOM, scoped navigation and resource rollback."""
from pathlib import Path
import subprocess

import pytest
from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope='module')
def article_bundle():
    entry = "import * as owner from './report-article'; (window as any).articleOwner=owner;"
    with compiled_typescript(ROOT / 'frontend/src/features/paper/report-reader.ts', contents=entry) as bundle:
        yield bundle


def run_article(bundle, body):
    setup = r"""
const fs=require('fs'),assert=require('node:assert/strict'),{JSDOM}=require(process.argv[1]+'/node_modules/jsdom');
const dom=new JSDOM('<body></body>',{url:'http://localhost/'}),w=dom.window;
global.window=w;global.document=w.document;global.Element=w.Element;global.AbortController=w.AbortController;
let live=0;const observers=[];
w.IntersectionObserver=class {
  constructor(callback){this.callback=callback;this.targets=[];this.closed=false;observers.push(this);live++;}
  observe(target){this.targets.push(target);}
  disconnect(){if(!this.closed){this.closed=true;live--;}}
};
(new Function(fs.readFileSync(process.argv[2],'utf8')))();
const owner=w.articleOwner;
function article(html){const a=document.createElement('article');a.innerHTML=html;document.body.append(a);return a;}
function host(){const h=document.createElement('section');h.className='paper-report-content';document.body.append(h);return h;}
const tr=()=> 'Enlarge image';
"""
    result = subprocess.run(['node', '-e', setup + body, str(ROOT), bundle], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_glossary_math_aliases_section_scope_and_prototype_names(article_bundle):
    run_article(article_bundle, r"""
const a=article(`<table><thead><tr><th>Term</th><th>Definition</th><th>Use</th><th>Analogy</th></tr></thead><tbody>
<tr><td>Test-Time Scaling (TTS)</td><td><strong>Visible definition</strong><span class="katex-mathml">DUPLICATE</span></td><td></td><td>Everyday analogy</td></tr>
<tr><td>constructor</td><td>Actual term definition</td></tr><tr><td>(term)</td><td>Placeholder</td></tr></tbody></table>
<h2>First</h2><p>TTS TTS TTSS constructor constructor</p><h3>Subsection</h3><p>TTS</p>
<h2>Second</h2><p>TTS <code>TTS</code><a>TTS</a><span class="katex">TTS</span></p>`);
owner.enhanceReportArticle(a,tr);
assert.equal(a.querySelectorAll('.paper-term').length,3);
assert.equal(a.querySelectorAll('.paper-term-card strong').length,2);
assert.equal(a.querySelector('.paper-term').getAttribute('aria-label'),'TTS: Visible definition');
assert.equal(a.querySelector('.paper-term-card-analogy').textContent,'💡 Everyday analogy');
assert.equal(a.querySelectorAll('code .paper-term,a .paper-term,.katex .paper-term').length,0);
owner.enhanceReportArticle(a,tr);assert.equal(a.querySelectorAll('.paper-term').length,3,'idempotent DOM enhancement');
""")


def test_figure_framing_preserves_prose_multiple_images_and_caption_text(article_bundle):
    run_article(article_bundle, r"""
const a=article('<p>Evidence before <img alt="Plot" src="/one"> evidence after.</p>'
 +'<p><img alt="First" src="/two"><img alt="Second" src="/three"></p>'
 +'<p><img alt="Fallback" src="/four"><em>Literal &lt;caption&gt;</em></p>');
owner.enhanceReportArticle(a,tr);
assert.equal(a.querySelectorAll('p').length,2);
assert.equal(a.querySelectorAll('img').length,4);
assert.equal(a.querySelectorAll('figure').length,1);
assert.ok(a.textContent.includes('Evidence before')&&a.textContent.includes('evidence after.'));
assert.equal(a.querySelector('figcaption').textContent,'Literal <caption>');
assert.equal(a.querySelector('caption'),null);
owner.enhanceReportArticle(a,tr);assert.equal(a.querySelectorAll('img').length,4);
""")


def test_heading_reservations_duplicates_and_safe_toc(article_bundle):
    run_article(article_bundle, r"""
const a=article('<span id="report-intro"></span><h2>Intro</h2><h2 id="custom">Second</h2><h3 id="custom">Third</h3>');
const entries=owner.enhanceReportArticle(a,tr);
assert.deepEqual(entries.map(e=>e.id),['report-intro-2','custom','custom-2']);
assert.deepEqual(owner.enhanceReportArticle(a,tr),entries);
const nav=document.createElement('div');
nav.innerHTML=owner.buildReportTableOfContents([{id:'quote" onclick="bad',text:'<img onerror=bad>',level:2},...entries],'"<Label>');
assert.equal(nav.querySelector('nav').getAttribute('aria-label'),'"<Label>');
assert.equal(nav.querySelectorAll('[onclick],img').length,0);
assert.equal(nav.querySelector('a').dataset.target,'quote" onclick="bad');
assert.equal(nav.querySelectorAll('[data-tofu-action]').length,0);
assert.equal(owner.buildReportTableOfContents(entries.slice(0,2),'Tiny'),'');
""")


def test_navigation_is_scoped_and_image_handlers_retire(article_bundle):
    run_article(article_bundle, r"""
let zoom=0;const ports={openImage:()=>zoom++,reducedMotion:()=>true};
const h1=host(),h2=host();
function fill(h){const a=document.createElement('article');a.innerHTML='<h2 id="same">Target</h2><h2>Middle</h2><h2>End</h2><p><img alt="Figure" src="/figure.png"></p>';
 const entries=owner.enhanceReportArticle(a,tr);h.innerHTML=owner.buildReportTableOfContents(entries,'Contents');h.append(a);return a;}
const a1=fill(h1),a2=fill(h2);let first=0,second=0;
h1.scrollTo=()=>first++;h2.scrollTo=opts=>{assert.equal(opts.behavior,'auto');second++;};
owner.mountReportArticle(h1,a1,ports);owner.mountReportArticle(h2,a2,ports);
h2.querySelector('a').click();assert.equal(first,0);assert.equal(second,1);assert.equal(document.activeElement,a2.querySelector('h2'));
const image=a2.querySelector('img');assert.equal(image.getAttribute('role'),'button');image.click();
image.dispatchEvent(new w.KeyboardEvent('keydown',{key:' ',bubbles:true,cancelable:true}));assert.equal(zoom,2);
observers[1].callback([{target:a2.querySelector('h2'),isIntersecting:true}]);
assert.equal(h2.querySelector('a').getAttribute('aria-current'),'location');
owner.releaseReportArticle(h2);image.click();h2.querySelector('a').click();assert.equal(zoom,2);assert.equal(second,1);
assert.equal(live,1);owner.destroyReportArticles();assert.equal(live,0);
""")


def test_remounts_and_observer_failure_roll_back_resources(article_bundle):
    run_article(article_bundle, r"""
const h=host(),a=document.createElement('article');a.innerHTML='<h2>A</h2><h2>B</h2><h2>C</h2><p><img src="/image.png"></p>';
const entries=owner.enhanceReportArticle(a,tr);h.innerHTML=owner.buildReportTableOfContents(entries,'Contents');h.append(a);
let zoom=0;const ports={openImage:()=>zoom++,reducedMotion:()=>true};
for(let i=0;i<60;i++){owner.mountReportArticle(h,a,ports);assert.equal(live,1);}
a.querySelector('img').click();assert.equal(zoom,1);owner.destroyReportArticles();assert.equal(live,0);
const Good=w.IntersectionObserver;
w.IntersectionObserver=class extends Good {observe(){throw Error('Observe failed');}};
assert.throws(()=>owner.mountReportArticle(h,a,ports),/Observe failed/);assert.equal(live,0);
a.querySelector('img').click();assert.equal(zoom,1,'failed mount retired its listener');
w.IntersectionObserver=Good;owner.mountReportArticle(h,a,ports);a.querySelector('img').click();assert.equal(zoom,2);
h.replaceChildren();a.querySelector('img').click();assert.equal(zoom,2,'removed article cannot act');
owner.destroyReportArticles();assert.equal(live,0);
""")


def test_retained_final_paint_releases_interactions_on_skeleton_abort_and_fallback(article_bundle):
    run_article(article_bundle, r"""
const source=fs.readFileSync(process.argv[1]+'/frontend/src/runtime/sections/paper/report.js','utf8');
const runtime={_captureReadingAnchor:()=>null,_loadReadingPosition:()=>null,_restoreReadingAnchor:()=>{},_persistReadingPosition:()=>{}};
const renderer=new Function('owner','runtimeScope',`
 const {enhanceReportArticle,buildReportTableOfContents,mountReportArticle,releaseReportArticle}=owner;
 const t=key=>key,escapeHtml=text=>String(text||''),prefersReducedMotion=()=>true;
 let renderMarkdown=text=>text;
 ${source}
 return {paint:_renderFinalReport,skeleton:_renderReportSkeleton,events:_paintReportFromState,
   finish:_teardownReadingTracker,disableMarkdown:()=>{renderMarkdown=undefined;}};
`)(owner,runtime);
const h=host();h.id='reportContent';
const view={kind:'report',containerId:h.id,idPrefix:'report',langKey:()=> 'en',stream:null};
const html='<h2>First</h2><h2>Second</h2><h2>Third</h2><p><img alt="Figure" src="/figure.png"></p>';
renderer.paint(h,html,null,view);assert.equal(live,1);assert.equal(h.querySelectorAll('figure').length,1);
renderer.skeleton(h,'en',view);assert.equal(live,0);
renderer.paint(h,html,null,view);assert.equal(live,1);
view.stream={status:'aborted',fullText:'Partial report',contentStarted:true};
renderer.events(view);assert.equal(live,0);assert.ok(h.textContent.includes('Partial report'));
view.stream=null;renderer.paint(h,html,null,view);assert.equal(live,1);
renderer.disableMarkdown();renderer.paint(h,'Plain fallback',null,view);assert.equal(live,0);
assert.equal(h.querySelector('pre').textContent,'Plain fallback');renderer.finish(true);
owner.destroyReportArticles();assert.equal(live,0);
""")
