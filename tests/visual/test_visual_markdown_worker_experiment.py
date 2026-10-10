"""Real-browser worker parser experiment keeps main-thread production HTML authority."""
import json
import os
import hashlib
import re
import tempfile
import threading
from pathlib import Path

import pytest

from tests._runtime_sections import ROOT,native_module_path
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin,frontend_visual_page as frontend_visual_page  # noqa:F401

pytestmark=pytest.mark.visual


def split_renderer_bundle():
    """Assert one preparation seam; reuse all remaining production projection code."""
    owner=ROOT/'frontend/src/markdown/index.ts'
    source=owner.read_text()
    boundary='const prepared = prepareMarkdownSource(text, escaped);'
    assert source.count(boundary)==1, 'production preparation boundary changed'
    source=source.replace(boundary,
        'const prepared = (globalThis as any).__workerPreparedSource?.(text, escaped) ?? prepareMarkdownSource(text, escaped);')
    source=re.sub(r"from '([^']+)'",lambda match: "from '"+str((owner.parent/match[1]).resolve())+"'",source)
    with tempfile.TemporaryDirectory(prefix='tofu-markdown-split-') as temporary:
        entry=Path(temporary)/'index.ts';entry.write_text(source)
        return native_module_path('.native/split-'+Path(temporary).name+'.js',entry)


def setup(page,origin):
    for path in ('node_modules/marked/lib/marked.umd.js','node_modules/dompurify/dist/purify.min.js',
                 'benchmarks/markdown_worker/client.js'):
        page.add_script_tag(path=str(ROOT/path))
    bundles={name:native_module_path(f'.native/worker-{name}.js',ROOT/path) for name,path in (
        ('policy','frontend/src/markdown-policy.ts'),('prepare','frontend/src/markdown/prepare-source.ts'),
        ('renderer','frontend/src/markdown/index.ts'))}
    bundles['renderer']=split_renderer_bundle()
    for path in bundles.values():page.add_script_tag(path=path)
    worker_source='importScripts('+json.dumps(origin+'/node_modules/marked/lib/marked.umd.js')+');\n'
    worker_source+='\n'.join(Path(bundles[name]).read_text() for name in ('policy','prepare'))
    worker_source+='''
installMarkdownPolicy(marked);
postMessage({ready:true});
onmessage=({data})=>{
  try {
    const started=performance.now();
    const prepared=prepareMarkdownSource(data.text,data.escapeRawHtml);
    const html=marked.parse(prepared.source);
    postMessage({id:data.id,source:prepared.source,math:prepared.math,html,elapsedMs:performance.now()-started});
  } catch(error) { postMessage({id:data.id,error:String(error)}); }
};
'''
    page.evaluate('installMarkdownPolicy(marked)')
    return worker_source


def test_worker_projection_matches_production_and_bounds_supersession(frontend_visual_page,frontend_visual_origin):
    page=frontend_visual_page;source=setup(page,frontend_visual_origin)
    result=page.evaluate(r'''async source=>{
      const worker=await createParserWorkerExperiment(source);
      let active=marked,latest=null;
      globalThis.__workerPreparedSource=(text,escaped)=>active===marked?undefined:{source:latest.source,math:latest.math};
      const proxy={parse(prepared){if(prepared!==latest.source)throw Error('mixed source');return latest.html;}};
      const renderer=createMarkdownRenderer({maximumCacheEntries:300,
        getEnvironment:()=>({document,parser:active,sanitizer:DOMPurify,highlighter:null,
          mathRenderer:null,basePath:'/fixture',projectActive:false}),ensureMath(){}});
      const fixtures=['plain 中文 paragraph','~~strict~~ and ~literal~',
        '[reference]\n\nTail.','[reference]\n\nTail.\n\n[reference]: /source',
        '```js\nconst x="<script>";\n','```js\nconst x=1;\n```\n\nNext.',
        '| name | value |\n| --- | --- |\n| a | 82\\|181 |',
        '<script>alert(1)</script><img src=x onerror=alert(1)>',
        '$a+b$ and $$c+d$$','- nested\n  - child\n\n#Header'];
      let equal=true;
      for(const text of fixtures){
        for(const escaped of [false,true]){
          active=marked;const reference=renderer.render(text,{streaming:true,escapeRawHtml:escaped});
          latest=await worker.parse(text,escaped);active=proxy;
          equal&&=renderer.render(text,{streaming:true,escapeRawHtml:escaped})===reference;
        }
      }
      const first=worker.parse('long source\n\n'.repeat(4000)).catch(error=>error.name);
      const superseded=worker.parse('old waiting').catch(error=>error.name);
      const newest=worker.parse('newest waiting');const bound=worker.pendingCount();
      const stale=await first;const cancelled=await superseded;const final=await newest;
      const pending=worker.parse('dispose source');worker.dispose();
      let disposed;try{await pending;}catch(error){disposed=error.name;}
      renderer.dispose();
      return {equal,bound,stale,cancelled,newest:final.source,disposed,pending:worker.pendingCount()};
    }''',source)
    assert result=={'equal':True,'bound':2,'stale':'AbortError','cancelled':'AbortError','newest':'newest waiting',
                    'disposed':'AbortError','pending':0}


def test_worker_paired_browser_measurement(browser,frontend_visual_origin):
    destination=os.environ.get('TOFU_MARKDOWN_WORKER_OUTPUT')
    if not destination:pytest.skip('opt-in independent-process worker measurement')
    report={'scope':'single worker preparation/parse plus asserted production projection and forced layout; CPU excludes semantic oracle',
        'whole_system_verdict':'incomplete','browser':browser.version,'cases':[],
        'sources':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
            'benchmarks/markdown_worker/client.js','frontend/src/markdown/index.ts',
            'frontend/src/markdown-policy.ts','frontend/src/markdown/prepare-source.ts')}}
    def run(variant,case,memory=False):
        owned=browser.browser_type.launch(headless=True,args=['--no-sandbox','--disable-gpu','--disable-dev-shm-usage'])
        try:
            page=owned.new_page(viewport={'width':1180,'height':900})
            page.goto(frontend_visual_origin,wait_until='load')
            source=setup(page,frontend_visual_origin)
            page.add_script_tag(path=native_module_path('.native/worker-text-blocks.js',ROOT/'frontend/src/conversation/ui/renderers/text-blocks.ts'))
            session=owned.new_browser_cdp_session()
            page_session=page.context.new_cdp_session(page)
            page_session.send('Performance.enable')
            peak=[0];sample_count=[0];stop_sampling=threading.Event();sampler=None
            if memory:
                import psutil
                renderer_ids=[int(row['id']) for row in session.send('SystemInfo.getProcessInfo')['processInfo'] if row['type']=='renderer']
                def sample():
                    while not stop_sampling.is_set():
                        rss=sum(psutil.Process(pid).memory_info().rss for pid in renderer_ids)
                        peak[0]=max(peak[0],rss);sample_count[0]+=1
                        stop_sampling.wait(0.005)
                sampler=threading.Thread(target=sample,daemon=True);sampler.start()
            start_cpu=sum(row['cpuTime'] for row in session.send('SystemInfo.getProcessInfo')['processInfo'])
            result=page.evaluate(r'''async ({source,variant,caseName})=>{
              const initialized=performance.now();
              const worker=variant==='worker'?await createParserWorkerExperiment(source):null;
              const startupMs=performance.now()-initialized;
              let latest=null,active=marked;
              globalThis.__workerPreparedSource=(text,escaped)=>active===marked?undefined:{source:latest.source,math:latest.math};
              const proxy={parse(prepared){if(prepared!==latest.source)throw Error('mixed source');return latest.html;}};
              const renderer=createMarkdownRenderer({maximumCacheEntries:300,
                getEnvironment:()=>({document,parser:active,sanitizer:DOMPurify,highlighter:null,
                  mathRenderer:null,basePath:'',projectActive:false}),ensureMath(){}});
              const node=document.createElement('section');document.body.append(node);
              const ports={renderSafeMarkdownHtml:(text,options)=>renderer.render(text,options)};
              const fixtures={
                short:'Small paragraph. ',
                prose:Array.from({length:64},(_,i)=>`Paragraph ${i}: `+'reader evidence 中文 '.repeat(32)).join('\n\n')+'\n\nTail ',
                code:Array.from({length:32},(_,i)=>'```js\n'+`const value_${i}=${i};\n`.repeat(8)+'```').join('\n\n')+'\n\nTail ',
                references:('[reference]\n\n| a | b |\n| --- | --- |\n| first | second |\n\n').repeat(32)+'Tail ',
                terminal:('Terminal paragraph with **markup**.\n\n').repeat(64),
              };
              const raw=[],responsiveness=[],observations=[];let equal=true,finalSource='';
              const terminal=caseName==='terminal';
              try{
                for(let index=0;index<120;index++){
                  const text=fixtures[caseName]+String(index)+(caseName==='references'&&index>=60?'\n\n[reference]: /source':'');
                  const probeStart=performance.now();
                  const probe=new Promise(resolve=>{setTimeout(()=>resolve(performance.now()-probeStart),0);});
                  const started=performance.now();
                  if(worker){latest=await worker.parse(text);active=proxy;}else active=marked;
                  renderTextBlock(node,{kind:'text',blockId:'fixture',markdown:text,terminal},ports);
                  node.getBoundingClientRect();
                  const duration=performance.now()-started;
                  const serviced=await probe;
                  if(index>=20){raw.push(duration);responsiveness.push(serviced);}
                  observations.push({text,html:node.querySelector('.md-content').innerHTML});
                  finalSource=text;
                }
                // Execute only AFTER Python takes the ending CPU/process measurement.
                globalThis.__checkWorkerOracle=()=>{
                  active=marked;renderer.clearCache();
                  for(const observed of observations){
                    const expected=document.createElement('div');
                    expected.innerHTML=renderer.render(observed.text,{streaming:!terminal});
                    if(observed.html!==expected.innerHTML)throw Error('worker DOM differs from production rendering');
                  }
                  renderer.dispose();delete globalThis.__workerPreparedSource;return observations.length;
                };
                const values=[...raw].sort((a,b)=>a-b),probes=[...responsiveness].sort((a,b)=>a-b);
                return {p50_ms:(values[49]+values[50])/2,p95_ms:values[94],p99_ms:values[98],
                  responsiveness_p99_ms:probes[98],raw,responsiveness,startupMs,equal,finalSource};
              }finally{worker?.dispose();node.remove();}
            }''',{'source':source,'variant':variant,'caseName':case})
            end_cpu=sum(row['cpuTime'] for row in session.send('SystemInfo.getProcessInfo')['processInfo'])
            stop_sampling.set()
            if sampler:sampler.join(timeout=2);assert not sampler.is_alive()
            result['cpu_ms_per_operation_including_warmup']=(end_cpu-start_cpu)*1000/120
            assert page.evaluate('__checkWorkerOracle()')==120
            result['final_source_sha256']=hashlib.sha256(result.pop('finalSource').encode()).hexdigest()
            if memory:
                assert sample_count[0]>0
                result={'sampled_peak_renderer_rss_bytes':peak[0],'rss_samples':sample_count[0]}
            return result
        finally:
            if 'stop_sampling' in locals():stop_sampling.set()
            if 'sampler' in locals() and sampler:sampler.join(timeout=2)
            owned.close()
    for case in ('short','prose','code','references','terminal'):
        pairs=[]
        for pair in range(3):
            values={}
            for variant in (('main','worker') if pair%2==0 else ('worker','main')):
                values[variant]=run(variant,case)
                values[variant].update(run(variant,case,memory=True))
            assert values['main']['final_source_sha256']==values['worker']['final_source_sha256']
            pairs.append(values)
        report['cases'].append({'case':case,'pairs':pairs})
        Path(destination).write_text(json.dumps(report,indent=2)+'\n')


def test_unhandled_worker_failure_closes_admission(frontend_visual_page):
    page=frontend_visual_page
    page.add_script_tag(path=str(ROOT/'benchmarks/markdown_worker/client.js'))
    result=page.evaluate('''async ()=>{
      const source="onmessage=()=>{throw new Error('injected worker failure')};postMessage({ready:true});";
      const worker=await createParserWorkerExperiment(source);
      const failed=await worker.parse('first').catch(error=>error.name);
      const closed=await worker.parse('later').catch(error=>error.name);
      const pending=worker.pendingCount();worker.dispose();
      return {failed,closed,pending};
    }''')
    assert result=={'failed':'Error','closed':'AbortError','pending':0}
