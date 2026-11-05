/** Goal journal: the five furniture places as tabs — material shelf, plan
 * board, execution desk, review desk and delivery table. Library and plan
 * render workspace records; execution, review and delivery host the run
 * inspector sections. Writes (materials, stop, reconcile) use injected ports
 * and are fenced by goal identity, so late results never repaint another goal.
 * Entry: mountGoalRoom.
 */
import type {EvolutionApi} from './controller';
import type {EvolutionRun,GoalWorkspace,GoalMaterial,MaterialInput,GoalCoordination} from '../../api/evolution-runs';
import {esc,patch} from './components/ui';
import {mountGoalRunInspector} from './studio/goal-run-inspector';
import type {OutcomeRead} from './studio/outcome-reader';
import {isTerminalRun} from './goal-workbench';
import {glyph} from './studio/glyphs';
import {PLACES,type Place} from './office-layout';

const KINDS=['all','paper','repository','web','dataset','other'];
const PLACE_GLYPH:Record<Place,string>={library:'book',plan:'board',work:'monitor',review:'check',delivery:'scroll'};
export function safeExternalUrl(value:string):string {
  try {const url=new URL(value);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password&&!/[\u0000-\u0020\\]/.test(value)?url.href:'';}catch{return '';}
}
export function mountGoalRoom(panel:HTMLElement,ports:{
  api:EvolutionApi;copy:(key:string)=>string;refresh:()=>Promise<unknown>;openTask:(id:string)=>void;
  stop:()=>Promise<void>;reconcile:()=>Promise<void>;download:()=>void;busy:()=>boolean;
  readOutcome?:OutcomeRead;visible:()=>boolean;selected?:(place:Place)=>void;
}){
  const {copy}=ports,text=(key:string)=>esc(copy(key));
  let run:EvolutionRun|undefined,workspace:GoalWorkspace={materials:[],stages:[],criteria:[]},coordination:GoalCoordination|undefined;
  let selected:Place='library',filter='all',disposed=false,identity='',stopArmed=false,loading=false;
  panel.classList.add('evo-journal');
  panel.innerHTML=`<nav class="evo-tabs" role="tablist" aria-label="${text('journal')}">${PLACES.map(place=>`<button type="button" role="tab" id="evo-tab-${place}" data-room-action="place" data-place="${place}" aria-controls="evo-tabpanel">${glyph(PLACE_GLYPH[place])}<span>${text('place_'+place)}</span><b></b></button>`).join('')}</nav>
    <div class="evo-tabpanel" id="evo-tabpanel" role="tabpanel"><div class="evo-tab-actions evo-button-row"></div><div class="evo-tab-static"></div>
    <div class="evo-tab-inspector" data-tab="work"></div><div class="evo-tab-inspector" data-tab="review"></div><div class="evo-tab-inspector" data-tab="delivery"></div></div>`;
  const tabpanel=panel.querySelector<HTMLElement>('.evo-tabpanel')!,actions=panel.querySelector<HTMLElement>('.evo-tab-actions')!;
  const staticHost=panel.querySelector<HTMLElement>('.evo-tab-static')!;
  const host=(tab:string)=>panel.querySelector<HTMLElement>(`.evo-tab-inspector[data-tab=${tab}]`)!;
  const inspectors={
    work:mountGoalRunInspector(host('work'),{copy,openTask:ports.openTask,sections:['status','latest','telemetry','records'],readOutcome:ports.readOutcome,visible:()=>ports.visible()&&selected==='work'}),
    review:mountGoalRunInspector(host('review'),{copy,openTask:ports.openTask,sections:['review']}),
    delivery:mountGoalRunInspector(host('delivery'),{copy,openTask:ports.openTask,sections:['deliverable'],deliverableId:'evo-deliverable'}),
  };
  function count(place:Place):string{
    if(!run)return '';
    if(place==='library')return String(workspace.materials.length||'');
    if(place==='plan')return String(workspace.stages.length||'');
    if(place==='work')return String(new Set([...run.receipts.map(r=>r.task_id),run.task_id].filter(Boolean)).size||'');
    return '';
  }
  function materialRow(item:GoalMaterial){
    const href=safeExternalUrl(item.url),domain=href?new URL(href).hostname:copy('invalidLink');
    return `<li data-key="${esc(item.material_id)}" data-origin="${esc(item.origin)}"><span class="evo-material-icon">${glyph(item.kind==='repository'?'monitor':item.kind==='paper'?'scroll':'book')}</span><div><a ${href?`href="${esc(href)}" target="_blank" rel="noopener noreferrer"`:'aria-disabled="true"'}>${esc(item.title)}</a><small>${text('kind_'+item.kind)} · ${esc(domain)} · ${text(item.origin==='agent'?'savedByAgent':'savedByYou')}</small>${item.note?`<p>${esc(item.note)}</p>`:''}</div><button type="button" class="evo-icon-button" data-room-action="remove" data-material="${esc(item.material_id)}" aria-label="${text('removeMaterial')}: ${esc(item.title)}" data-tip="${text('removeMaterial')}">${glyph('close')}</button></li>`;
  }
  function staticContent():string{
    if(!run)return '';
    if(selected==='library'){
      const items=workspace.materials.filter(item=>filter==='all'||item.kind===filter);
      return `<div class="evo-filters" role="group" aria-label="${text('filterMaterials')}">${KINDS.map(kind=>`<button type="button" data-room-action="filter" data-filter="${kind}" aria-pressed="${filter===kind}">${text('kind_'+kind)}<b>${kind==='all'?workspace.materials.length:workspace.materials.filter(m=>m.kind===kind).length}</b></button>`).join('')}</div>
        ${items.length?`<ul class="evo-materials">${items.map(materialRow).join('')}</ul>`:'<p class="evo-empty-mark">—</p>'}
        <details class="evo-material-add" data-key="add-${esc(run.run_id)}" data-evo-island="true"><summary>${glyph('plus')}<span>${text('addMaterial')}</span></summary><form data-run="${esc(run.run_id)}"><label>${text('materialTitle')}<input name="title" required maxlength="240"></label><label>${text('materialUrl')}<input name="url" type="url" required maxlength="2000" placeholder="https://"></label><label>${text('materialKind')}<select name="kind">${KINDS.slice(1).map(k=>`<option value="${k}">${text('kind_'+k)}</option>`).join('')}</select></label><label>${text('materialNote')}<textarea name="note" maxlength="500" rows="2"></textarea></label><button class="evo-button evo-primary" type="submit">${text('saveMaterial')}</button><p role="status"></p></form></details>`;
    }
    if(selected==='plan')return `<section class="evo-ledger-section"><h3>${text('objective')}</h3><p class="evo-objective-text">${esc(run.objective)}</p></section>
      <section class="evo-ledger-section"><h3>${text('acceptance')}</h3>${workspace.criteria.length?`<ul class="evo-checks">${workspace.criteria.map(c=>`<li><strong>${esc(c.requirement)}</strong></li>`).join('')}</ul>`:'<p class="evo-empty-mark">—</p>'}</section>
      <section class="evo-ledger-section"><h3>${text('plannedSteps')}</h3>${workspace.stages.length?`<ol class="evo-steps">${workspace.stages.map(s=>`<li><strong>${esc(s.kind)}</strong><p>${esc(s.detail)}</p></li>`).join('')}</ol>`:'<p class="evo-empty-mark">—</p>'}</section>`;
    return '';
  }
  function renderActions(){
    const buttons:string[]=[];
    if(run&&selected==='work'){
      if(!isTerminalRun(run))buttons.push(`<button type="button" class="evo-button evo-danger" data-room-action="stop" data-armed="${stopArmed}" ${ports.busy()?'disabled':''}>${glyph('stop')}<span>${text(stopArmed?'confirmStop':'cancel')}</span></button>`);
      buttons.push(`<button type="button" class="evo-button" data-room-action="reconcile" ${ports.busy()?'disabled':''}>${glyph('refresh')}<span>${text('reconcile')}</span></button>`);
      if(run.task_id)buttons.push(`<button type="button" class="evo-button" data-room-action="task" data-task="${esc(run.task_id)}">${glyph('arrow')}<span>${text('missionOpenExecution')}</span></button>`);
    }
    if(run&&selected==='delivery'&&run.result&&!run.compute_wait)buttons.push(`<button type="button" class="evo-button" data-room-action="download">${glyph('download')}<span>${text('download')}</span></button>`);
    patch(actions,buttons.join(''));actions.hidden=!buttons.length;
  }
  function render(){
    if(disposed)return;
    panel.querySelectorAll<HTMLButtonElement>('[role=tab]').forEach(tab=>{
      const place=tab.dataset.place as Place,on=place===selected;
      tab.setAttribute('aria-selected',String(on));tab.tabIndex=on?0:-1;
      tab.querySelector('b')!.textContent=count(place);
      if(place==='review')tab.dataset.verdict=run?.review?.outcome||'';
      if(place==='delivery')tab.dataset.ready=String(!!run?.result&&!run.compute_wait);
    });
    tabpanel.setAttribute('aria-labelledby',`evo-tab-${selected}`);
    patch(staticHost,staticContent());staticHost.hidden=!['library','plan'].includes(selected);
    for(const tab of ['work','review','delivery'] as const){host(tab).hidden=selected!==tab;}
    const observation={run,workspace,coordination,loading};
    inspectors.work.update(observation);inspectors.review.update(observation);inspectors.delivery.update(observation);
    inspectors.work.visibilityChanged(ports.visible()&&selected==='work');
    renderActions();
  }
  async function click(event:Event){
    const target=(event.target as Element).closest<HTMLElement>('[data-room-action]');if(!target||!panel.contains(target))return;
    const action=target.dataset.roomAction;
    if(action==='place'){open(target.dataset.place as Place);return;}
    if(action==='filter'){filter=target.dataset.filter||'all';render();return;}
    if(action==='task'){if(target.dataset.task)ports.openTask(target.dataset.task);return;}
    if(action==='download'){ports.download();return;}
    if(action==='stop'){
      if(!stopArmed){stopArmed=true;render();panel.querySelector<HTMLElement>('[data-room-action=stop]')?.focus({preventScroll:true});return;}
      stopArmed=false;await ports.stop();return;
    }
    if(action==='reconcile'){await ports.reconcile();return;}
    if(action==='remove'&&run){
      const id=run.run_id;target.setAttribute('disabled','');
      try{await ports.api.removeMaterial(id,target.dataset.material!);if(!disposed&&run?.run_id===id)await ports.refresh();}
      catch(e){if(!disposed&&run?.run_id===id){target.setAttribute('aria-label',String(e instanceof Error?e.message:e));target.removeAttribute('disabled');}}
    }
  }
  function keydown(event:KeyboardEvent){
    const tab=(event.target as Element).closest<HTMLElement>('[role=tab]');if(!tab)return;
    const delta=event.key==='ArrowRight'?1:event.key==='ArrowLeft'?-1:0;if(!delta)return;
    event.preventDefault();
    const next=PLACES[(PLACES.indexOf(selected)+delta+PLACES.length)%PLACES.length];
    open(next);panel.querySelector<HTMLElement>(`#evo-tab-${next}`)?.focus();
  }
  async function submit(event:Event){
    const form=event.target as HTMLFormElement;if(!panel.contains(form)||!form.dataset.run)return;
    event.preventDefault();event.stopPropagation();
    if(!run||form.dataset.run!==run.run_id)return;
    const id=run.run_id,button=form.querySelector<HTMLButtonElement>('[type=submit]')!;
    if(button.disabled)return;button.disabled=true;
    const value=(key:string)=>(form.elements.namedItem(key) as HTMLInputElement).value.trim();
    const body={title:value('title'),url:value('url'),kind:value('kind'),note:value('note')} as MaterialInput;
    try{await ports.api.saveMaterial(id,body);if(disposed||run?.run_id!==id)return;form.reset();form.closest('details')!.open=false;await ports.refresh();}
    catch(e){if(!disposed&&run?.run_id===id)form.querySelector('[role=status]')!.textContent=e instanceof Error?e.message:String(e);}
    finally{if(!disposed&&form.isConnected)button.disabled=false;}
  }
  function open(place:Place){
    if(selected!==place){selected=place;stopArmed=false;}
    render();ports.selected?.(place);
  }
  panel.addEventListener('submit',submit);panel.addEventListener('click',click);panel.addEventListener('keydown',keydown);
  return {
    open,
    selected(){return selected;},
    update(next:EvolutionRun|undefined,data:GoalWorkspace|undefined,journal:GoalCoordination|undefined,pending=false){
      run=next;workspace=data||{materials:[],stages:[],criteria:[]};coordination=journal;loading=pending;
      if(identity!==(run?.run_id||'')){identity=run?.run_id||'';filter='all';stopArmed=false;staticHost.replaceChildren();}
      render();
    },
    visibilityChanged(){inspectors.work.visibilityChanged(ports.visible()&&selected==='work');},
    dispose(){disposed=true;Object.values(inspectors).forEach(item=>item.dispose());panel.removeEventListener('submit',submit);panel.removeEventListener('click',click);panel.removeEventListener('keydown',keydown);},
  };
}
