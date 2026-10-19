// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderRichResponse, disposeRichResponse, metricStatus } from './renderer';
import { parseRichResponse, splitRichMarkdown, validateRichResponse } from './validation';
import { renderRichMarkdown, disposeRichMarkdown } from './markdown';
import { projectRichToolResult } from './tool-result';
import { renderTextBlock } from '../conversation/ui/renderers/text-blocks';
import type { RichResponse } from './contract.generated';

function payload(): RichResponse {
  return {version:1,title:'Performance review',summary:'Measured historical cohort',blocks:[
    {type:'comparison',id:'compare',title:'Latency',baselineLabel:'SQLite',candidateLabel:'TofuDB',metrics:[
      {label:'Stream P99',baseline:2707,candidate:1267,unit:'ms',direction:'lower',status:'failed',budget:1000}]},
    {type:'chart',id:'chart',title:'Chart',metrics:[{label:'Stream',baseline:2707,candidate:1267,unit:'ms',direction:'lower',status:'failed'}]},
    {type:'table',id:'table',title:'Samples',columns:['Backend','Latency'],rows:[['TofuDB','1267']]},
    {type:'evidence',id:'evidence',title:'Provenance',items:[{label:'Build',value:'historical only'}]},
    {type:'artifact_reference',id:'artifact',title:'Full report',artifactId:'report'},
    {type:'action_panel',id:'actions',title:'Actions',actions:[{kind:'open_artifact',label:'Open report',targetId:'report'}]},
  ]};
}
function host() {const node=document.createElement('div');document.body.append(node);return node;}
const ports = {renderSafeMarkdownHtml:(text:string)=>`<p>${text.replaceAll('<','&lt;')}</p>`,localizedText:(_key:string,fallback:string)=>fallback};
afterEach(()=>{document.body.replaceChildren();vi.restoreAllMocks();});

describe('native contract and untrusted content',()=>{
  it('supports six textual block families and inert malicious prose',()=>{
    const response=payload();response.title='<img src=x onerror=alert(1)>';
    const root=host();expect(renderRichResponse(root,response)).toBe(true);
    expect(root.querySelectorAll('.rich-block')).toHaveLength(6);
    expect(root.querySelectorAll('script,img')).toHaveLength(0);
    expect(root.textContent).toContain('<img src=x onerror=alert(1)>');
    expect(root.querySelector('[data-status=failed]')).not.toBeNull();
    expect(root.textContent).toContain('relative improvement');
    disposeRichResponse(root);
  });
  it.each(['version','duplicate','width','nan','action','extra'])('rejects %s',kind=>{
    const response:any=payload();
    if(kind==='version')response.version=2;
    if(kind==='duplicate')response.blocks.push(response.blocks[0]);
    if(kind==='width')response.blocks[2].rows=[['extra','columns','bad']];
    if(kind==='nan')response.blocks[0].metrics[0].candidate=NaN;
    if(kind==='action')response.blocks[5].actions[0].kind='execute_javascript';
    if(kind==='extra')response.css='body{display:none}';
    expect(validateRichResponse(response)).toBeNull();
  });
  it('refuses contradictory pass and undefined ratios',()=>{
    const metric:any={label:'latency',baseline:0,candidate:1267,unit:'ms',direction:'lower',status:'passed',budget:1000};
    expect(metricStatus(metric)).toBe('failed');
    const response=payload();(response.blocks[0] as any).metrics=[metric];
    const root=host();renderRichResponse(root,response);
    expect(root.textContent).toContain('zero baseline');expect(root.textContent).not.toContain('Infinity');
  });
  it('parses complete fences while preserving ordinary code',()=>{
    expect(parseRichResponse('x'.repeat(256001))).toBeNull();
    const fence='```tofu-ui\n'+JSON.stringify(payload())+'\n```';
    expect(splitRichMarkdown('```text\n'+fence+'\n```').some(p=>p.kind==='rich')).toBe(false);
    expect(splitRichMarkdown(Array(10).fill(fence).join('\n')).filter(p=>p.kind==='rich')).toHaveLength(10);
    expect(splitRichMarkdown('```tofu-ui\n{"version":1}')[0].kind).toBe('partial');
  });
  it('projects native/MCP data without activating remote UI resources',()=>{
    expect(projectRichToolResult([{structuredContent:{richResponse:payload()}}])?.title).toBe('Performance review');
    expect(projectRichToolResult([{_meta:{ui:{resourceUri:'https://evil.example/widget'}}}])).toBeNull();
    expect(projectRichToolResult([{content:JSON.stringify({richResponse:payload()})}])).not.toBeNull();
  });
});

describe('mounted lifecycle',()=>{
  it('retains unchanged nodes and compatible focus/open state on changed evidence',()=>{
    const root=host(),response=payload();renderRichResponse(root,response,{conversationId:'owned'});
    const details=root.querySelector<HTMLDetailsElement>('[data-rich-block=actions]')!;
    const control=details.querySelector<HTMLButtonElement>('button')!;
    details.open=true;control.focus();
    renderRichResponse(root,JSON.parse(JSON.stringify(response)),{conversationId:'owned'});
    expect(root.querySelector('[data-rich-block=actions]')).toBe(details);
    expect(document.activeElement).toBe(control);
    (response.blocks[5] as any).title='Updated actions';renderRichResponse(root,response,{conversationId:'owned'});
    expect(root.querySelector<HTMLDetailsElement>('[data-rich-block=actions]')!.open).toBe(true);
    expect((document.activeElement as HTMLElement).dataset.richControl).toBe('open_artifact:report');
  });
  it('streams incomplete fences then reconstructs cards from original source',()=>{
    const root=host();renderRichMarkdown(root,'Intro\n```tofu-ui\n{',false,ports as any);
    expect(root.textContent).toContain('still arriving');
    const text='Intro\n```tofu-ui\n'+JSON.stringify(payload())+'\n```\nConclusion';
    renderRichMarkdown(root,text,true,ports as any);expect(root.querySelector('.rich-metric')).not.toBeNull();
    const clone=host();renderRichMarkdown(clone,text,true,ports as any);expect(clone.textContent).toBe(root.textContent);
    disposeRichMarkdown(root);disposeRichMarkdown(clone);
  });
  it('keeps human-authored UI fences inert and invalid source inspectable',()=>{
    const node=host();
    const text='```tofu-ui\n'+JSON.stringify(payload())+'\n```';
    renderTextBlock(node,{kind:'text',blockId:'answer',markdown:text,terminal:true,deliverable:true} as any,ports as any,{actor:'human',source:{projection:{}}} as any);
    expect(node.querySelector('.rich-response')).toBeNull();
    const root=host();renderRichMarkdown(root,'```tofu-ui\n{"version":99}\n```',true,ports as any);
    expect(root.textContent).toContain('99');expect(root.textContent).toContain('unsupported');
  });
  it('authorizes artifact actions against the originating conversation',async()=>{
    const open=vi.fn(),authorize=vi.fn(async()=>({})),root=host();
    renderRichResponse(root,payload(),{conversationId:'owned',authorizeArtifact:authorize,openArtifact:open});
    root.querySelector<HTMLButtonElement>('[data-rich-control="open_artifact:report"]')!.click();
    await Promise.resolve();
    expect(authorize).toHaveBeenCalledWith('report','owned',expect.any(AbortSignal));
    expect(open).toHaveBeenCalledWith('report');disposeRichResponse(root);
  });
  it('refuses foreign artifact actions and disables unscoped actions',async()=>{
    const root=host(),open=vi.fn();renderRichResponse(root,payload(),{openArtifact:open});
    expect(root.querySelector<HTMLButtonElement>('[data-rich-control="open_artifact:report"]')!.disabled).toBe(true);
    const denied=host();renderRichResponse(denied,payload(),{conversationId:'owned',openArtifact:open,authorizeArtifact:async()=>{throw Error('Unavailable');}});
    denied.querySelector<HTMLButtonElement>('[data-rich-control="open_artifact:report"]')!.click();
    await Promise.resolve();await Promise.resolve();expect(open).not.toHaveBeenCalled();
    expect(denied.textContent).toContain('unavailable');disposeRichResponse(root);disposeRichResponse(denied);
  });
  it('aborts queued image work on disposal and ignores late delivery',async()=>{
    const response=payload();response.blocks=[{type:'image_gallery',id:'gallery',title:'Images',images:[{id:'asset',alt:'chart'}]}];
    let resolve!: (blob:Blob)=>void;let signal!:AbortSignal;
    const root=host();const load=vi.fn((_id:string,s:AbortSignal)=>{signal=s;return new Promise<Blob>(r=>{resolve=r;});});
    renderRichResponse(root,response,{asset:load});await Promise.resolve();
    expect(load).toHaveBeenCalledOnce();disposeRichResponse(root);expect(signal.aborted).toBe(true);
    resolve(new Blob(['x'],{type:'image/png'}));await Promise.resolve();expect(root.querySelector('img')).toBeNull();
  });
  it('refuses HTML masquerading as an image and exposes retry',async()=>{
    const response=payload();response.blocks=[{type:'image_gallery',id:'gallery',title:'Images',images:[{id:'asset',alt:'chart'}]}];
    const root=host();renderRichResponse(root,response,{asset:async()=>new Blob(['<html/>'],{type:'text/html'})});
    await Promise.resolve();await Promise.resolve();await Promise.resolve();
    expect(root.querySelector('img')).toBeNull();expect(root.textContent).toContain('unavailable');
    expect(root.querySelector<HTMLButtonElement>('[data-rich-control="retry:asset"]')!.hidden).toBe(false);disposeRichResponse(root);
  });
});

import timelineFixture from '../../../designs/ui-gallery/live-photo-timeline.fixture.json';
function timing(): any { return structuredClone(timelineFixture); }
describe('native timing diagrams', () => {
  it('draws adjacent capture intervals on one track with complete semantic evidence', () => {
    const root = host(); expect(renderRichResponse(root, timing())).toBe(true);
    const intervals = root.querySelectorAll<SVGRectElement>('.rich-timeline-video');
    expect(intervals).toHaveLength(2);
    expect(intervals[0].getAttribute('y')).toBe(intervals[1].getAttribute('y'));
    expect(root.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
    expect(root.querySelector('table')?.textContent).toContain('Ring buffer');
    expect(root.textContent).toContain('Shutter');
    expect(root.querySelector('svg')?.outerHTML).not.toMatch(/NaN|Infinity/);
  });
  it('stacks overlapping intervals without changing authored evidence order', () => {
    const response = timing(); response.blocks[0].lanes[0].intervals[1].start = -1;
    const root = host(); renderRichResponse(root, response);
    const intervals = root.querySelectorAll<SVGRectElement>('.rich-timeline-video');
    expect(intervals[0].getAttribute('y')).not.toBe(intervals[1].getAttribute('y'));
    expect(root.querySelector('tbody')?.textContent).toMatch(/Ring buffer.*Continued capture/);
  });
  it.each(['axis', 'interval', 'outside', 'marker', 'lane', 'duplicate', 'nan', 'styles'])('rejects invalid %s', kind => {
    const response = timing(), block = response.blocks[0];
    if (kind === 'axis') block.axis.end = block.axis.start;
    if (kind === 'interval') block.lanes[0].intervals[0].end = -2;
    if (kind === 'outside') block.lanes[0].intervals[0].start = -3;
    if (kind === 'marker') block.markers[0].time = 3;
    if (kind === 'lane') block.markers[0].laneId = 'missing';
    if (kind === 'duplicate') block.lanes.push(block.lanes[0]);
    if (kind === 'nan') block.axis.start = NaN;
    if (kind === 'styles') block.css = 'body{display:none}';
    expect(validateRichResponse(response)).toBeNull();
  });
  it('keeps malicious labels inert and preserves disclosure/focus after updates', () => {
    const response = timing(); response.blocks[0].lanes[0].label = '<script>alert(1)</script>';
    const root = host(); renderRichResponse(root, response);
    expect(root.querySelector('script')).toBeNull();
    const data = root.querySelector<HTMLDetailsElement>('.rich-timeline-data')!;
    data.open = true; data.querySelector<HTMLElement>('summary')!.focus();
    response.blocks[0].title = 'Updated timing'; renderRichResponse(root, response);
    expect(root.querySelector<HTMLDetailsElement>('.rich-timeline-data')!.open).toBe(true);
    expect((document.activeElement as HTMLElement).dataset.richControl).toBe('timeline-data');
    expect(root.textContent).toContain('<script>alert(1)</script>');
  });
});


it('parses and mounts valid JSON and messages beyond retired byte ceilings', () => {
  const response: RichResponse = {version:1,title:'Complete',blocks:[{type:'table',id:'large',title:'Complete table',columns:Array(12).fill('Column'),rows:Array.from({length:100},(_,row)=>Array.from({length:12},(_,col)=>`${row}:${col}:`+'x'.repeat(980)))}]};
  const source=JSON.stringify(response);
  expect(source.length).toBeGreaterThan(1024*1024);
  expect(parseRichResponse(source)).toEqual(response);
  expect(projectRichToolResult([{content:JSON.stringify({richResponse:response})}])).toEqual(response);
  const text=Array.from({length:9},(_,i)=>'```tofu-ui\n'+JSON.stringify({...response,title:'Complete '+i})+'\n```').join('\n');
  expect(splitRichMarkdown(text).filter(p=>p.kind==='rich')).toHaveLength(9);
  const node=host();renderRichMarkdown(node,'```tofu-ui\n'+source+'\n```',true,ports as any);
  expect(node.querySelectorAll('tbody tr')).toHaveLength(100);
  expect(node.textContent).toContain('99:11:');
  disposeRichMarkdown(node);renderRichMarkdown(node,'```tofu-ui\n'+JSON.stringify(payload())+'\n```',true,ports as any);
  expect(node.querySelectorAll('.rich-block')).toHaveLength(6);disposeRichMarkdown(node);
});


it('projects complete tool candidates and deep wrappers while terminating cycles', () => {
  let wrapped: unknown = {richResponse:payload()};
  for(let i=0;i<64;i+=1) wrapped={result:wrapped};
  expect(projectRichToolResult([...Array(40).fill({content:'ordinary'}),wrapped])).toEqual(payload());
  const loop: Record<string,unknown>={};loop.result=loop;
  expect(projectRichToolResult([loop])).toBeNull();
  expect(projectRichToolResult([loop,{structuredContent:{richResponse:payload()}}])).toEqual(payload());
});


it('starts every image independently and aborts all pending loads at teardown',async()=>{
  const response:RichResponse={version:1,title:'Images',blocks:[{type:'image_gallery',id:'all',title:'All',images:Array.from({length:8},(_,i)=>({id:'image'+i,alt:'Image '+i}))}]};
  const signals:AbortSignal[]=[];const root=host();
  renderRichResponse(root,response,{asset:(_id,signal)=>{signals.push(signal);return new Promise(()=>{});}});
  await Promise.resolve();expect(signals).toHaveLength(8);
  disposeRichResponse(root);expect(signals.every(signal=>signal.aborted)).toBe(true);
});


it('retains every rich block, table column and row beyond retired counts',()=>{
  const table={type:'table' as const,id:'table',title:'Complete',columns:Array(13).fill('Column'),rows:Array.from({length:101},(_,r)=>Array.from({length:13},(_,c)=>`${r}:${c}`))};
  const response:RichResponse={version:1,title:'Complete',blocks:Array.from({length:25},(_,i)=>({...table,id:'table'+i}))};
  expect(validateRichResponse(response)).not.toBeNull();
  const node=host();renderRichResponse(node,response);
  expect(node.querySelectorAll('.rich-block')).toHaveLength(25);
  expect(node.querySelector('[data-rich-block="table24"]')!.textContent).toContain('100:12');
  disposeRichResponse(node);
});


it('retains timing markers above the retired count',()=>{
  const response=timing();response.blocks[0].markers=Array(33).fill(response.blocks[0].markers[0]);
  expect(validateRichResponse(response)).not.toBeNull();
  const root=host();renderRichResponse(root,response);
  expect(root.querySelectorAll('.rich-timeline-marker')).toHaveLength(33);disposeRichResponse(root);
});
