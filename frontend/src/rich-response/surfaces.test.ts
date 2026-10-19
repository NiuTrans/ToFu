// @vitest-environment jsdom
import {afterEach,describe,expect,it,vi} from 'vitest';
import {renderRichResponse,disposeRichResponse} from './renderer';
import {validateRichResponse} from './validation';
import {ResourceHub} from './resource-client';
import {renderGuidanceForm} from './guidance-form';
import type {ChoicePorts,ChoiceInteraction} from './choices';

const snapshot={fetchedAt:'2026-10-15T00:00:00Z',rows:[{id:'gpu',queue:'training',kind:'gpu' as const,name:'H200',unit:'count',total:8,allocated:8,available:0},
  {id:'other',queue:'research',kind:'gpu' as const,name:'H200',unit:'count',total:8,allocated:0,available:8}]};
const block={type:'resource_panel' as const,id:'capacity',title:'Capacity',provider:'test',snapshot};
const text=(_key:string,fallback:string)=>fallback;
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
afterEach(()=>{document.querySelectorAll<HTMLElement>('.rich-response').forEach(disposeRichResponse);document.body.replaceChildren();vi.useRealTimers();vi.restoreAllMocks();});

describe('surface contract and stable layout',()=>{
  it('rejects ambiguous layouts, negative resources and invalid maxima',()=>{
    const payload={version:1,title:'Capacity',blocks:[block],layout:{mode:'tabs',groups:[{id:'one',title:'One',blocks:['missing']}]}};
    expect(validateRichResponse(payload)).toBeNull();
    payload.layout.groups[0].blocks=['capacity'];expect(validateRichResponse(payload)).not.toBeNull();
    expect(validateRichResponse({...payload,blocks:[{...block,snapshot:{...snapshot,rows:[{...snapshot.rows[0],available:-1}]}}]})).toBeNull();
    expect(validateRichResponse({version:1,title:'Stats',blocks:[{type:'metric_grid',id:'m',title:'Stats',items:[{id:'m',label:'x',unit:'',value:0,maximum:0}]}]})).toBeNull();
  });
  it('preserves the active tab, search and focus across snapshot updates',()=>{
    const host=document.createElement('div');document.body.append(host);
    const payload={version:1,title:'Dashboard',layout:{mode:'tabs',groups:[{id:'overview',title:'Overview',blocks:['note']},{id:'resources',title:'Resources',blocks:['capacity']}]},
      blocks:[{type:'notice',id:'note',title:'Note',message:'Waiting for resources',tone:'info'},block]};
    expect(renderRichResponse(host,payload)).toBe(true);
    (host.querySelectorAll<HTMLButtonElement>('[role="tab"]')[1]).click();
    const search=host.querySelector<HTMLInputElement>('[data-rich-control="resource-search"]')!;
    search.value='training';search.dispatchEvent(new Event('input'));search.focus();
    renderRichResponse(host,{...payload,blocks:[payload.blocks[0],{...block,snapshot:{...snapshot,rows:[{...snapshot.rows[0],available:2}]}}]});
    expect(host.querySelector('[data-rich-control="resource-search"]')).toBe(search);expect(document.activeElement).toBe(search);
    expect(host.querySelector('[role="tab"][aria-selected="true"]')?.textContent).toBe('Resources');
    expect(host.querySelector('tbody')?.textContent).toContain('2 GPUs');
  });
  it('renders unknown independently from zero and keeps values inert',()=>{
    const host=document.createElement('div');document.body.append(host);
    renderRichResponse(host,{version:1,title:'Stats',blocks:[{type:'metric_grid',id:'m',title:'Stats',items:[
      {id:'unknown',label:'Unknown capacity',value:null,unit:'GPU'},{id:'zero',label:'<img onerror=alert(1)>',value:0,unit:'GPU'}]}]});
    expect(host.textContent).toContain('Unknown');expect(host.textContent).toContain('0 GPU');expect(host.querySelector('img')).toBeNull();
  });
});

describe('resource request lifetime',()=>{
  it('shares reads, preserves failed snapshots, pauses hidden reads and releases every timer',async()=>{
    vi.useFakeTimers();Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
    const read=vi.fn().mockResolvedValue(snapshot),hub=new ResourceHub(document,read);
    const a=vi.fn(),b=vi.fn();const first=hub.subscribe({runId:'owned'},'test',a),second=hub.subscribe({runId:'owned'},'test',b);
    first.active(true);second.active(true);await flush();expect(read).toHaveBeenCalledTimes(1);
    first.live(true);expect(vi.getTimerCount()).toBe(1);
    read.mockRejectedValueOnce(new Error('offline'));first.refresh();await flush();expect(a.mock.lastCall?.[0]).toEqual({phase:'error',snapshot});
    Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(90000);expect(read).toHaveBeenCalledTimes(2);expect(vi.getTimerCount()).toBe(0);
    first.dispose();second.dispose();expect(vi.getTimerCount()).toBe(0);
  });
  it('rejects late results after final release and isolates distinct owners/scopes',async()=>{
    Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
    const resolvers:Array<(value:typeof snapshot)=>void>=[],signals:AbortSignal[]=[];
    const hub=new ResourceHub(document,(_scope,_provider,signal)=>{signals.push(signal);return new Promise(resolve=>resolvers.push(resolve));});
    const listener=vi.fn(),other=vi.fn();const one=hub.subscribe({runId:'one'},'test',listener),two=hub.subscribe({runId:'two'},'test',other);
    one.active(true);two.active(true);await flush();expect(signals).toHaveLength(2);one.dispose();expect(signals[0].aborted).toBe(true);
    const count=listener.mock.calls.length;resolvers[0](snapshot);await flush();expect(listener).toHaveBeenCalledTimes(count);
    resolvers[1](snapshot);await flush();expect(other.mock.lastCall?.[0].phase).toBe('ready');two.dispose();
  });
});

describe('durable task forms',()=>{
  it('requires explicit answers and preserves a draft and command across an uncertain save',async()=>{
    const value:ChoiceInteraction={interactionId:'form',mode:'batch',question:'Configure',options:[],questions:[
      {question:'Queue',options:['Training','Research']},{question:'GPU hours',options:[]}],state:'pending'};
    const submit=vi.fn().mockRejectedValueOnce(new Error('ack lost')).mockImplementation(async(_id,response)=>({...value,state:'answered',response}));
    const ports:ChoicePorts={load:async()=>value,submit,commandId:vi.fn(()=> 'same-command'),text};
    const view=renderGuidanceForm(document,{type:'form_prompt',id:'form',title:'Configure',interactionId:'form'},ports);document.body.append(view.node);await flush();
    const button=view.node.querySelector<HTMLButtonElement>('[type="submit"]')!;expect(button.disabled).toBe(true);
    const select=view.node.querySelector('select')!;select.value='Training';select.dispatchEvent(new Event('change',{bubbles:true}));
    const input=view.node.querySelector('input')!;input.value='24';input.dispatchEvent(new Event('input',{bubbles:true}));expect(button.disabled).toBe(false);
    view.node.dispatchEvent(new Event('submit',{cancelable:true}));await flush();expect(input.value).toBe('24');expect(view.node.textContent).toContain('could not be confirmed');
    view.node.dispatchEvent(new Event('submit',{cancelable:true}));await flush();expect(submit.mock.calls.map(call=>call[2])).toEqual(['same-command','same-command']);expect(view.node.textContent).toContain('Answer saved');
    expect(view.node.querySelector('fieldset')?.disabled).toBe(true);view.dispose();
  });
});

it('keeps a form draft and focus when the live Turn projection advances',async()=>{
  const value:ChoiceInteraction={interactionId:'form',mode:'free_text',question:'Budget',options:[],state:'pending'};
  const ports:ChoicePorts={load:async()=>value,submit:vi.fn(),commandId:()=> 'command',text};
  const host=document.createElement('div');document.body.append(host);
  const payload={version:1,title:'Configure',blocks:[{type:'form_prompt',id:'form',title:'Budget',interactionId:'form'}]};
  renderRichResponse(host,payload,{choices:ports,conversationId:'owned',interactionSnapshot:'1'});await flush();
  const input=host.querySelector('input')!;input.value='24';input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();
  renderRichResponse(host,payload,{choices:ports,conversationId:'owned',interactionSnapshot:'2'});await flush();
  expect(host.querySelector('input')).toBe(input);expect(input.value).toBe('24');expect(document.activeElement).toBe(input);
});

it('keeps 100 identical views on one read and timer, with no unrelated refresh',async()=>{
  vi.useFakeTimers();Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});
  const read=vi.fn().mockResolvedValue(snapshot),hub=new ResourceHub(document,read);
  const views=Array.from({length:100},()=>hub.subscribe({runId:'owned'},'test',()=>{}));
  views.forEach(view=>{view.active(true);view.live(true);});await flush();
  expect(read).toHaveBeenCalledTimes(1);expect(vi.getTimerCount()).toBe(1);
  const other=hub.subscribe({runId:'other'},'test',()=>{});other.active(true);await flush();
  expect(read).toHaveBeenCalledTimes(2);other.dispose();views.forEach(view=>view.dispose());expect(vi.getTimerCount()).toBe(0);
});

it('renders the largest admitted sparkline values without invalid SVG coordinates',()=>{
  const host=document.createElement('div');document.body.append(host);
  renderRichResponse(host,{version:1,title:'Stats',blocks:[{type:'metric_grid',id:'m',title:'Stats',items:[
    {id:'m',label:'Range',unit:'',value:0,history:[-1e12,0,1e12]}]}]});
  expect(host.querySelector('polyline')?.getAttribute('points')).not.toMatch(/NaN|Infinity/);
});
