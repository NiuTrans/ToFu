/** Lazy resource entry for owned task surfaces. Provider discovery occurs only
 * after opening; selection/run changes dispose every subscription and request.
 */
import { createButton, createField } from '../ui';
import { request } from '../api/transport';
import { renderRichResponse, disposeRichResponse } from './renderer';
import type { SurfaceContext } from './resource-client';
import type { SurfaceText } from './surface-blocks';

export function mountResourceLauncher(host:HTMLElement,text:SurfaceText){
  const doc=host.ownerDocument,details=doc.createElement('details'),summary=doc.createElement('summary');
  summary.textContent=text('rich.surface.explore','Resources');details.className='rich-resource-launcher';
  const controls=doc.createElement('div');controls.className='ui-toolbar';
  const status=doc.createElement('p');status.setAttribute('role','status');
  const retry=createButton({label:text('rich.surface.refresh','Refresh'),size:'sm'},doc);
  const content=doc.createElement('div');details.append(summary,controls,status,content);host.append(details);
  let context:SurfaceContext|undefined,identity='',flight:AbortController|undefined,loaded=false,disposed=false;
  function render(provider:string,connection?:string,queue?:string){
    if(!context)return;
    renderRichResponse(content,{version:1,title:text('rich.surface.explore','Resources'),blocks:[
      {type:'resource_panel',id:'resources',title:connection?`${provider} · ${connection}`:provider,provider,...(connection?{connection}:{}),...(queue?{queue}:{})}]},{surfaceContext:context,text});
  }
  async function load(){
    if(!context||disposed||flight||!details.open)return;
    const controller=new AbortController();flight=controller;status.textContent=text('rich.surface.loading','Reading provider resources…');retry.disabled=true;
    try{
      const result=await request<{providers:Array<{id:string;connection?:string;requiresQueue?:boolean}>}>('/api/response-surfaces/providers?'+new URLSearchParams(context),{signal:controller.signal});
      if(controller.signal.aborted||disposed||flight!==controller)return;
      if(!Array.isArray(result.providers)||result.providers.some(value=>!value||typeof value.id!=='string'||!/^[A-Za-z0-9_-]{1,64}$/.test(value.id)
        ||(value.connection!==undefined&&(typeof value.connection!=='string'||!/^[A-Za-z0-9_.-]{1,128}$/.test(value.connection)))
        ||(value.requiresQueue!==undefined&&typeof value.requiresQueue!=='boolean')||(value.requiresQueue&&!value.connection))
        ||new Set(result.providers.map(value=>JSON.stringify([value.id,value.connection]))).size!==result.providers.length)throw new Error('Invalid provider catalog');
      loaded=true;controls.replaceChildren();status.textContent=result.providers.length?'':text('rich.surface.noProviders','No compute provider is configured for this application.');
      if(result.providers.length){
        const field=createField({label:text('rich.surface.provider','Resource platform'),choices:result.providers.map(value=>({value:JSON.stringify(value),label:value.connection?`${value.id} · ${value.connection}`:value.id}))},doc);
        const select=field.control as HTMLSelectElement;const queueField=createField({label:text('rich.surface.queueName','Queue name')},doc);
        const queueInput=queueField.control as HTMLInputElement;queueInput.maxLength=256;
        const open=createButton({label:text('rich.surface.openResources','View resources'),size:'sm'},doc);
        const selectProvider=()=>{const value=JSON.parse(select.value);queueField.el.hidden=!value.requiresQueue;open.hidden=!value.requiresQueue;
          disposeRichResponse(content);content.replaceChildren();if(!value.requiresQueue)render(value.id,value.connection);};
        open.addEventListener('click',()=>{const value=JSON.parse(select.value);if(queueInput.value.trim()){
          render(value.id,value.connection,queueInput.value.trim());content.tabIndex=-1;
          content.focus({preventScroll:true});content.scrollIntoView({block:'start'});
        }});
        controls.append(field.el,queueField.el,open);
        select.addEventListener('change',selectProvider);selectProvider();
      }
      controls.append(retry);
    }catch{if(!controller.signal.aborted&&!disposed){status.textContent=text('rich.surface.readError','Resources unavailable; retry.');controls.replaceChildren(retry);}}
    finally{if(flight===controller){flight=undefined;retry.disabled=false;}}
  }
  const refresh=()=>void load();retry.addEventListener('click',refresh);
  const toggle=()=>{
    if(details.open){if(!loaded)void load();}
    else{flight?.abort();flight=undefined;disposeRichResponse(content);content.replaceChildren();loaded=false;}
  };details.addEventListener('toggle',toggle);
  return {update(value:SurfaceContext|undefined){
    const next=JSON.stringify(value??null);if(next===identity)return;
    identity=next;context=value;flight?.abort();flight=undefined;loaded=false;
    disposeRichResponse(content);content.replaceChildren();controls.replaceChildren();status.textContent='';details.hidden=!context;
    if(details.open)void load();
  },dispose(){disposed=true;flight?.abort();disposeRichResponse(content);retry.removeEventListener('click',refresh);details.removeEventListener('toggle',toggle);details.remove();}};
}
