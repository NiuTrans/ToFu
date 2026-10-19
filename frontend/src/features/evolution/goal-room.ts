/** Goal-scoped furniture navigation and material shelf. API is injected.
 * No global goal state; late mutations cannot repaint another goal's panel.
 */
import type {EvolutionApi} from './controller';
import type {EvolutionRun,GoalWorkspace,GoalMaterial,MaterialInput} from '../../api/evolution-runs';
import {esc,patch} from './components/ui';

import { mountGoalRunInspector } from './studio/goal-run-inspector';
import { reviewOutcomeLabel } from './goal-workbench';
import { createButton } from '../../ui/index';
type Place='library'|'plan'|'work'|'review'|'delivery';
const PLACES:Place[]=['library','plan','work','review','delivery'];
const KINDS=['all','paper','repository','web','dataset','other'];
const ICONS:Record<Place,string>={library:'book',plan:'board',work:'monitor',review:'check',delivery:'file'};
const PATHS:Record<string,string>={
 book:'M12 5v15M12 6C9 3 5 3 2 5v14c4-2 7-2 10 1 3-3 6-3 10-1V5c-3-2-7-2-10 1Z',
 board:'M4 3h16v15H4zM8 7h8M8 11h5M8 18l-2 4m10-4 2 4',
 monitor:'M3 4h18v13H3zM8 21h8m-4-4v4',
 check:'M4 3h16v18H4zM8 11l3 3 5-6',
 file:'M5 2h9l5 5v15H5zM14 2v6h5M8 12h8M8 16h6',
};
export const roomIcon=(name:string)=>`<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="${PATHS[name]||PATHS.file}"/></svg>`;
export function safeExternalUrl(value:string):string {
  try {const url=new URL(value);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password&&!/[\u0000-\u0020\\]/.test(value)?url.href:'';}catch{return '';}
}
export function mountGoalRoom(panel:HTMLElement,hotspots:HTMLElement,shortcuts:HTMLElement,ports:{
  api:EvolutionApi;copy:(key:string)=>string;refresh:()=>Promise<unknown>;openTask:(id:string)=>void;readDelivery:()=>void;openPanel?:(title:string)=>void;create?:()=>void;
}){
  const {copy}=ports;const text=(key:string)=>esc(copy(key));
  let run:EvolutionRun|undefined,workspace:GoalWorkspace={materials:[],stages:[],criteria:[]};
  let selected:Place='library',filter='all',disposed=false,identity='';
  let exploring=false;

  let inspectionHost:HTMLElement|undefined,inspector:ReturnType<typeof mountGoalRunInspector>|undefined;
  function openPlace(place:Place){
    selected=place;exploring=true;render();ports.openPanel?.(copy('place_'+selected));
    if(!ports.openPanel)panel.querySelector<HTMLElement>('#evo-room-title')?.focus({preventScroll:true});
  }
  function count(place:Place){return place==='library'?workspace.materials.length:place==='plan'?workspace.stages.length:place==='delivery'?(run?.result?1:0):place==='review'?(run?.review?1:0):new Set([...(run?.receipts.map(r=>r.task_id)||[]),run?.task_id].filter(Boolean)).size;}
  function materialRow(item:GoalMaterial){
    const href=safeExternalUrl(item.url),domain=href?new URL(href).hostname:copy('invalidLink');
    return `<li data-key="${esc(item.material_id)}"><div class="evo-material-icon">${roomIcon(item.kind==='repository'?'monitor':'file')}</div><div><a ${href?`href="${esc(href)}" target="_blank" rel="noopener noreferrer"`:'aria-disabled="true"'}>${esc(item.title)} <span aria-hidden="true">↗</span></a><small>${text('kind_'+item.kind)} · ${esc(domain)}</small>${item.note?`<p>${esc(item.note)}</p>`:''}<div class="evo-material-meta"><small>${text(item.origin==='agent'?'savedByAgent':'savedByYou')}</small><button type="button" data-room-action="remove" data-material="${esc(item.material_id)}" aria-label="${text('removeMaterial')}: ${esc(item.title)}">${text('removeMaterial')}</button></div></div></li>`;
  }
  function content(){
    if(!run)return `<p class="evo-room-empty">${text('roomPurpose_'+selected)}</p><div class="evo-room-intro-actions"></div>`;
    if(selected==='library'){
      const items=workspace.materials.filter(item=>filter==='all'||item.kind===filter);
      return `<div class="evo-material-filters" role="group" aria-label="${text('filterMaterials')}">${KINDS.map(kind=>`<button type="button" data-room-action="filter" data-filter="${kind}" aria-pressed="${filter===kind}">${text('kind_'+kind)} <span>${kind==='all'?workspace.materials.length:workspace.materials.filter(m=>m.kind===kind).length}</span></button>`).join('')}</div><ul class="evo-material-list">${items.map(materialRow).join('')}</ul>${items.length?'':`<p class="evo-room-empty">${text(workspace.materials.length?'noMatchingMaterials':'shelfEmpty')}</p>`}<p class="evo-room-note">${text('shelfEvidenceNote')}</p><details class="evo-material-add" data-key="add-${esc(run.run_id)}" data-evo-island="true"><summary>＋ ${text('addMaterial')}</summary><form data-run="${esc(run.run_id)}"><label>${text('materialTitle')}<input name="title" required maxlength="240"></label><label>${text('materialUrl')}<input name="url" type="url" required maxlength="2000" placeholder="https://"></label><label>${text('materialKind')}<select name="kind">${KINDS.slice(1).map(k=>`<option value="${k}">${text('kind_'+k)}</option>`).join('')}</select></label><label>${text('materialNote')}<textarea name="note" maxlength="500" rows="2"></textarea></label><button class="evo-button evo-primary" type="submit">${text('saveMaterial')}</button><p role="status"></p></form></details>`;
    }
    if(selected==='plan')return `<h3>${text('objective')}</h3><p class="evo-room-objective">${esc(run.objective)}</p><h3>${text('acceptance')}</h3><ul class="evo-room-checks">${workspace.criteria.map(c=>`<li>${esc(c.requirement)}</li>`).join('')}</ul><h3>${text('plannedSteps')}</h3><ol class="evo-room-steps">${workspace.stages.map(s=>`<li><strong>${esc(s.kind)}</strong><p>${esc(s.detail)}</p></li>`).join('')}</ol>${workspace.stages.length?'':`<p class="evo-room-empty">${text('planPending')}</p>`}`;
    if(selected==='work')return '<div class="evo-work-inspector" data-key="work-inspector" data-evo-island="true"></div>';
    if(selected==='review')return run.review?`<p class="evo-room-state">${esc(reviewOutcomeLabel(run.review.outcome,copy))}</p><ul class="evo-room-checks">${run.review.criterion_results.map(c=>`<li><strong>${esc(workspace.criteria.find(item=>item.criterion_id===c.criterion_id)?.requirement||c.criterion_id)} · ${esc(reviewOutcomeLabel(c.outcome,copy))}</strong><p>${esc(c.evidence_ref)}</p></li>`).join('')}</ul>`:`<p class="evo-room-empty">${text('reviewPending')}</p>`;
    return run.result?`<p class="evo-room-note">${text('deliveryForGoal')}</p><pre class="evo-room-result">${esc(run.result.slice(0,1600))}${run.result.length>1600?'…':''}</pre><button class="evo-button evo-primary" data-room-action="delivery">${text('readDelivery')} ↓</button>`:`<p class="evo-room-empty">${text('deliveryEmpty')}</p>`;
  }
  function render(){
    if(disposed)return;
    const buttons=(scene:boolean)=>PLACES.map(place=>`<button type="button" class="${scene?'evo-object evo-object-'+place:'evo-place-shortcut'}" data-room-action="place" data-place="${place}" id="evo-${scene?'object':'shortcut'}-${place}" aria-label="${text('place_'+place)} · ${count(place)}" aria-controls="evo-goal-panel" aria-pressed="${selected===place}" ${run||ports.create?'':'disabled'}><span>${roomIcon(ICONS[place])}<span>${text('place_'+place)}</span><b>${count(place)}</b></span></button>${scene?`<wa-tooltip for="evo-object-${place}" placement="bottom">${text('place_'+place)} · ${count(place)}</wa-tooltip>`:''}`).join('');
    patch(hotspots,buttons(true));patch(shortcuts,buttons(false));
    panel.hidden=!run&&!exploring;
    patch(panel,`<header class="evo-room-heading">${roomIcon(ICONS[selected])}<div><h2 id="evo-room-title" tabindex="-1">${text('place_'+selected)}</h2><p class="evo-room-goal-name" title="${esc(run?.objective||'')}">${esc(run?.objective||copy('onlyThisGoal'))}</p></div></header><div class="evo-room-content">${content()}</div>`);
    const intro=panel.querySelector<HTMLElement>('.evo-room-intro-actions');
    if(intro&&ports.create)intro.replaceChildren(createButton({label:copy('newGoal'),variant:'primary',onClick:ports.create},panel.ownerDocument));

    const workHost=panel.querySelector<HTMLElement>('.evo-work-inspector');
    if(workHost){if(inspectionHost!==workHost){inspectionHost=workHost;inspector=mountGoalRunInspector(workHost,{copy,openTask:ports.openTask,detailed:true});}inspector?.update({run,workspace});}
  }
  async function click(event:Event){
    const target=(event.target as Element).closest<HTMLElement>('[data-room-action]');if(!target)return;
    const action=target.dataset.roomAction;
    if(action==='place'){
      openPlace(target.dataset.place as Place);return;
    }
    if(action==='filter'){filter=target.dataset.filter||'all';render();return;}
    if(action==='task'){if(target.dataset.task)ports.openTask(target.dataset.task);return;}
    if(action==='delivery'){ports.readDelivery();return;}
    if(action==='remove'&&run){
      const id=run.run_id;target.setAttribute('disabled','');
      try{await ports.api.removeMaterial(id,target.dataset.material!);if(!disposed&&run?.run_id===id)await ports.refresh();}
      catch(e){if(!disposed&&run?.run_id===id){target.textContent=String(e instanceof Error?e.message:e);target.removeAttribute('disabled');}}
    }
  }
  async function submit(event:Event){
    event.preventDefault();event.stopPropagation();const form=event.target as HTMLFormElement;
    if(!run||form.dataset.run!==run.run_id)return;
    const id=run.run_id,button=form.querySelector<HTMLButtonElement>('[type=submit]')!;
    if(button.disabled)return;button.disabled=true;
    const value=(key:string)=>(form.elements.namedItem(key) as HTMLInputElement).value.trim();
    const body={title:value('title'),url:value('url'),kind:value('kind'),note:value('note')} as MaterialInput;
    try{await ports.api.saveMaterial(id,body);if(disposed||run?.run_id!==id)return;form.reset();form.closest('details')!.open=false;await ports.refresh();}
    catch(e){if(!disposed&&run?.run_id===id)form.querySelector('[role=status]')!.textContent=e instanceof Error?e.message:String(e);}
    finally{if(!disposed&&form.isConnected)button.disabled=false;}
  }
  panel.addEventListener('submit',submit);[panel,hotspots,shortcuts].forEach(node=>node.addEventListener('click',click));
  return {
    open:openPlace,
    update(next:EvolutionRun|undefined,data?:GoalWorkspace){
      run=next;workspace=data||{materials:[],stages:[],criteria:[]};
      if(identity!==(run?.run_id||'')){identity=run?.run_id||'';filter='all';selected='library';exploring=false;panel.replaceChildren();}
      render();
    },
    dispose(){disposed=true;panel.removeEventListener('submit',submit);[panel,hotspots,shortcuts].forEach(node=>node.removeEventListener('click',click));},
  };
}
