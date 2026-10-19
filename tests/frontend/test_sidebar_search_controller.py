"""Execute the TypeScript owner and retained sidebar adapter without a build."""
import subprocess
from pathlib import Path
import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_global_sidebar_pagination_with_search_bridge():
    # The older harness predates the typed composition bridge; keep its positive
    # and fault-injected assertions, but supply the missing composition ports.
    from tests.frontend.test_frontend_sidebar_pagination import _HARNESS

    source = ROOT / 'frontend/src/runtime/sections/ui/conversation_list.js'
    harness = _HARNESS.replace(
        "eval(fs.readFileSync(process.argv[2], 'utf8'));",
        "global.createSidebarSearchController = () => ({clear(){},destroy(){}});\n"
        "global.retainedCompositionLifecycle = {add(){},listen(){}};\n"
        "global.sidebarSearchQuery = '';\n"
        "eval(fs.readFileSync(process.argv[1], 'utf8'));",
    ).replace("process.argv[3] === 'neuter'", "process.argv[2] === 'neuter'")
    for neuter in (False, True):
        result = subprocess.run(
            ['node', '-e', harness, str(source)] + (['neuter'] if neuter else []),
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        if neuter:
            assert 'FAIL older_page_merged_c6' in result.stdout
            assert 'FAIL all_9_loaded' in result.stdout
        else:
            assert 'FAIL ' not in result.stdout, result.stdout
            assert 'PASS all_9_loaded' in result.stdout


def test_sidebar_search_controller_behavior():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync('frontend/src/sidebar-search-controller.ts', 'utf8');
const compiled = ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS, target:ts.ScriptTarget.ES2022}}).outputText;
const mod = {exports:{}};
vm.runInNewContext(compiled, {exports:mod.exports, AbortController, setTimeout, clearTimeout, Date, Map});
const create = mod.exports.createSidebarSearchController;
const hit = (id, title=id) => ({id, matchField:'content', matchSnippet:'needle', conversation:{id,title,rev:1,msgCount:2}});
const page = (items=[], nextCursor=null, coverage='ready') => ({ok:true,items,nextCursor,coverage,coverageReasons:[]});
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
 let time=0, calls=[], paints=[];
 const c=create({debounceMs:0, now:()=>time, render:s=>paints.push(s), fetchPage:(q,cursor,signal)=>new Promise((resolve,reject)=>calls.push({q,cursor,signal,resolve,reject}))});
 c.setQuery('a',[hit('local')],'v1');
 for(let i=0;i<100;i++) c.setQuery('a',[hit('arrived')],'v1');
 assert.equal(calls.length,1);
 calls[0].resolve(page([hit('unloaded')], 'next','partial')); await tick();
 assert.equal(c.snapshot().items[0].id,'unloaded');
 c.setQuery('a',[hit('arrived')],'v2');
 assert.equal(c.snapshot().items[0].id,'unloaded'); assert.equal(calls.length,1);
 time=30000; c.setQuery('a',[],'v3'); assert.equal(calls.length,2);
 calls[1].reject(new Error('offline')); await tick();
 assert.equal(c.snapshot().error,true); assert.equal(c.snapshot().items[0].id,'unloaded');
 c.retry(); assert.equal(calls.length,3); // No 30s metadata-refresh wait.
 for(let i=0;i<100;i++) c.retry(); assert.equal(calls.length,3);
 calls[2].resolve(page([hit('recovered')])); await tick(); assert.equal(c.snapshot().error,false);
 c.setQuery('b'); assert.equal(calls.length,4);
 c.setQuery('a'); c.setQuery('b'); assert.equal(calls.length,4); assert.equal(calls[3].signal.aborted,true);
 calls[3].reject(new Error('stale ABA failure')); await tick();
 assert.equal(calls.length,5); assert.equal(c.snapshot().error,false);
 calls[4].resolve(page([hit('latest')])); await tick(); assert.equal(c.snapshot().items[0].id,'latest');
 c.setQuery('cancel'); const pending=calls.at(-1); c.clear(); assert.equal(pending.signal.aborted,true);
 pending.resolve(page([hit('stale')])); await tick(); assert.equal(c.snapshot().query,'');
 c.setQuery('destroy'); const last=calls.at(-1); c.destroy(); last.reject(Error('late')); await tick(); assert.equal(c.snapshot().query,'');
 // Continuation failures retain membership and retry exactly the failed cursor.
 let retryCalls=[], clock=0, timers=[];
 const retryMod={exports:{}};
 vm.runInNewContext(compiled,{exports:retryMod.exports,AbortController,Date,Map,
   setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout:()=>{}});
 const r=retryMod.exports.createSidebarSearchController({debounceMs:0,now:()=>clock,render:()=>{},
   fetchPage:(q,cursor)=>new Promise((resolve,reject)=>retryCalls.push({cursor,resolve,reject}))});
 r.setQuery('retry'); retryCalls[0].resolve(page([hit('kept')],'continuation')); await tick();
 r.loadMore(); retryCalls[1].reject(Error('offline')); await tick();
 r.retry(); assert.equal(retryCalls[2].cursor,'continuation');
 retryCalls[2].reject(Error('still offline')); await tick();
 for(let i=0;i<100;i++) r.retry();
 assert.equal(retryCalls.length,3); assert.equal(timers.length,1); assert.equal(timers[0].ms,1000);
 clock=1000; timers[0].fn(); assert.equal(retryCalls.length,4);
 retryCalls[3].resolve(page([hit('next')])); await tick();
 assert.equal(r.snapshot().items.map(h=>h.id).join(','),'kept,next'); r.destroy();
 let n=0;
 const p=create({debounceMs:0,render:()=>{},fetchPage:async()=>{n++;return page([],String(n),'partial')}});
 p.setQuery('q'); await tick(); assert.equal(n,4); assert.equal(p.snapshot().nextCursor,'4');
 p.loadMore(); await tick(); assert.equal(n,8); p.destroy();
 let count=0;
 const bounded=create({debounceMs:0,render:()=>{},fetchPage:async()=>page(Array.from({length:200},(_,i)=>hit(String(count++))), 'more')});
 bounded.setQuery('q'); await tick(); bounded.loadMore(); await tick(); bounded.loadMore(); await tick();
 assert.equal(bounded.snapshot().items.length,500);
 assert.equal(bounded.snapshot().truncated,true);
 assert.equal(bounded.snapshot().nextCursor,null); bounded.loadMore(); assert.equal(count,600); bounded.destroy();
 // Real retained presentation must render and open unloaded IDs without paging the catalog.
 const dom = new JSDOM('<div id="convList"></div><div id="sidebarSearchStats"></div>');
 const document=dom.window.document; let rendered, merged=[];
 const retained=fs.readFileSync('frontend/src/runtime/sections/ui/conversation_list.js','utf8');
 const bridge=retained.slice(retained.indexOf('const _sidebarSearchController ='),retained.indexOf('function searchByTitle'));
 const catalog=[{id:'newest',updatedAt:9000},{id:'boundary',updatedAt:7000}];
 const ctx={document, sidebarSearchQuery:'needle', conversations:catalog,
 createSidebarSearchController:()=>({snapshot:()=>({items:[hit('remote')]}),destroy(){},retry(){},loadMore(){}}),
 retainedCompositionLifecycle:{add(){},listen(target,type,fn,opts){target.addEventListener(type,fn,opts)}},
 mergeConversationCatalogRows:rows=>{merged.push(...rows);catalog.push(...rows)},
 t:k=>k, _renderSearchResults:()=>{}, Api:{conversations:{}}};
 // Capture the actual adapter render callback, leaving the request owner tested above.
 ctx.createSidebarSearchController=ports=>{rendered=ports.render;return {snapshot:()=>({items:[hit('remote')]}),destroy(){},retry(){},loadMore(){}}};
 ctx._renderSearchResults=results=>{document.getElementById('convList').innerHTML=results.map(r=>`<div class="conv-item" data-conv-id="${r.conv.id}">${r.conv.title}</div>`).join('')};
 ctx.getServerTotalCount=()=>9;
 ctx.renderConversationList=()=>{};
 ctx.createConversationSettingsResolution=()=>({});
 ctx.runtimeScope={};
 const projectOwner={exports:{}};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/conversation/application/conversation-project.ts','utf8'),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText, {exports:projectOwner.exports});
 ctx.protectHydratingProjectSettings=projectOwner.exports.protectHydratingProjectSettings;
 vm.runInNewContext(fs.readFileSync('frontend/src/runtime/sections/core/conv_apply_settings.js','utf8'),ctx);
 const catalogSource=fs.readFileSync('frontend/src/runtime/sections/core/conversation_catalog.js','utf8');
 vm.runInNewContext(catalogSource.slice(0,catalogSource.indexOf('/** Paint cached')),ctx);
 ctx.createSidebarSearchController=ports=>{rendered=ports.render;return {snapshot:()=>({items:[{...hit('remote'),conversation:{...hit('remote').conversation,updatedAt:1000,settings:{}}}]}),destroy(){},retry(){},loadMore(){}}};
 vm.runInNewContext(retained.slice(retained.indexOf('let _loadingMoreGlobalConvs'),retained.indexOf('function renderConversationList()')),ctx);
 vm.runInNewContext(bridge,ctx);
 rendered({query:'needle',items:[hit('remote','Remote title')],loading:false,error:false,coverage:'partial',nextCursor:'c'});
 assert.match(document.getElementById('convList').textContent,/Remote title/);
 assert.equal(catalog.length,2);
 document.querySelector('.conv-item').dispatchEvent(new dom.window.Event('pointerdown',{bubbles:true}));
 assert.equal(catalog.length,2); // A settings-less search hit is not canonical metadata.
 const navigationModule={exports:{}};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync('frontend/src/features/project-conversation-navigation.ts','utf8'),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:navigationModule.exports});
 const navigation=navigationModule.exports.createProjectConversationNavigation();
 let opened, metadataReads=0;
 document.addEventListener('click',()=>navigation.resolveMissing('remote',{
   contains:id=>catalog.some(c=>c.id===id),
   fetch:async id=>{metadataReads++;return {id,title:'Empty worker',updatedAt:1000,msgCount:0,rev:0,settings:{projectPath:'/project'}}},
   merge:row=>{vm.runInNewContext('_globalConvPageBoundary ||= _oldestGlobalConvBoundary(conversations)',ctx);ctx.mergeConversationCatalogRows([row])},
   navigate:id=>{opened=catalog.find(c=>c.id===id)},error:error=>{throw error},
 }));
 document.querySelector('.conv-item').dispatchEvent(new dom.window.Event('click',{bubbles:true})); await tick();
 const remote=catalog.find(c=>c.id==='remote');
 assert.equal(metadataReads,1); assert.equal(remote._serverTurnCount,0); assert.equal(remote._serverRev,0);
 assert.equal(remote.projectPath,'/project'); assert.equal(remote._localOnly,false);
 assert.equal(opened,remote); assert.equal(catalog.length,3);
 let pagerCalls=0;
 ctx.sidebarSearchQuery='';
 ctx.Api.conversations.listPage=async(ts,id)=>{pagerCalls++;assert.equal(ts,7000);assert.equal(id,'boundary');return {items:[{id:'middle',updatedAt:6000}]}};
 await ctx.loadMoreGlobalConvs(); assert.equal(pagerCalls,1); assert.ok(catalog.some(c=>c.id==='middle'));
 ctx.sidebarSearchQuery='needle';
 remote.title='Arriving metadata'; remote._serverRev=2;
 rendered({query:'needle',items:[hit('remote')],loading:false,error:true,coverage:'partial',nextCursor:'c'});
 assert.match(document.getElementById('convList').textContent,/Arriving metadata/);
 assert.match(document.getElementById('sidebarSearchStats').textContent,/searchError/);
 assert.equal(document.querySelectorAll('button').length,2);
 rendered({query:'needle',items:[hit('remote')],loading:false,error:false,coverage:'ready',nextCursor:null,truncated:true});
 assert.match(document.getElementById('sidebarSearchStats').textContent,/searchTruncated/);
 vm.runInNewContext(retained.slice(retained.indexOf('function searchByTitle')),ctx);
 catalog.push({id:'worker-prefixed-id',title:'Unrelated title',_serverRev:0,_serverTurnCount:0});
 assert.equal(ctx.searchByTitle('worker-prefixed-id')[0].conv.id,'worker-prefixed-id');
 const purgeSource=fs.readFileSync('frontend/src/runtime/sections/main.js','utf8');
 vm.runInNewContext(purgeSource.slice(purgeSource.indexOf('function _purgeEmptyConvs()'),purgeSource.indexOf('// ── Per-conversation')),ctx);
 ctx.activeConvId='newest'; ctx.runtimeScope.ConversationTurnRead={ordered:()=>[]};
 ctx._purgeEmptyConvs();
 assert.ok(ctx.conversations.some(item=>item.id==='worker-prefixed-id'));
 assert.ok(ctx.conversations.some(item=>item.id==='remote'));
 console.log('sidebar search behavior passed');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
