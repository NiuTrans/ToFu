/** Role model inspector. Chat catalog only; live tasks retain their pinned model.
 * No polling or credential state. Late writes cannot repaint a different goal.
 */
import { createButton, createField } from '../../ui/index';
import type { EvolutionApi } from './controller';
import type { EvolutionModel, EvolutionRole, EvolutionRun, RoleModels } from '../../api/evolution-runs';

export function mountRoleModels(host:HTMLElement, ports:{
  copy:(key:string)=>string;api:EvolutionApi;models:()=>Array<{model:EvolutionModel;label:string}>;
  ready:()=>boolean;current:()=>EvolutionRun|undefined;draft:()=>RoleModels;setDraft:(value:RoleModels)=>void;
  refresh:()=>Promise<void>;openTask:(id:string)=>void;askHuman:()=>void;
}) {
  let role:EvolutionRole='executor', runId='', disposed=false, saving=false;
  const title=document.createElement('h2'), hint=document.createElement('p');
  hint.textContent=ports.copy('roleModelHint');
  const select=document.createElement('select');select.dataset.roleModel='true';
  const field=createField({label:ports.copy('model'),control:select});
  const selectShell=document.createElement('div');selectShell.className='ui-select';
  select.replaceWith(selectShell);selectShell.append(select);
  const status=document.createElement('p');status.setAttribute('role','status');
  const save=createButton({label:ports.copy('saveRoleModel'),variant:'primary',onClick:()=>{void submit();}});
  const task=createButton({label:ports.copy('inspectTask'),onClick:()=>{
    const run=ports.current();if(run?.run_id!==runId)return;
    const stage=role==='executor'?'executing':'reviewing';
    const id=run.status===stage?run.task_id:[...run.receipts].reverse().find(r=>r.stage===stage)?.task_id;
    if(id)ports.openTask(id);
  }});
  const human=createButton({label:ports.copy('askHuman'),onClick:ports.askHuman});
  host.append(title,hint,field.el,save,status,task,human);
  function refreshOptions(){
    if(disposed||saving)return;
    const run=ports.current();if((run?.run_id||'')!==runId)return;
    const selected=(run?.role_models||ports.draft())[role];
    select.replaceChildren(new Option(ports.copy('inheritGoalModel'),''));
    ports.models().forEach((entry,i)=>select.add(new Option(entry.label,String(i))));
    const index=ports.models().findIndex(entry=>JSON.stringify(entry.model)===JSON.stringify(selected));
    if(selected&&index<0){select.add(new Option(ports.copy('modelUnavailable'),'unavailable'));select.value='unavailable';}
    else select.value=selected?String(index):'';
    save.disabled=!ports.ready()||!ports.models().length||select.value==='unavailable';
  }
  select.addEventListener('change',()=>{save.disabled=saving||!ports.ready()||select.value==='unavailable';});
  async function submit(){
    if(saving||disposed||!ports.ready()||(ports.current()?.run_id||'')!==runId)return;
    const ownerRun=runId, ownerRole=role;
    const chosen=select.value===''?null:ports.models()[Number(select.value)]?.model;
    if(chosen===undefined)return;
    saving=true;save.disabled=true;select.disabled=true;status.textContent='';
    try{
      if(ownerRun){await ports.api.updateRoleModel(ownerRun,ownerRole,chosen);await ports.refresh();}
      else {const draft={...ports.draft()};if(chosen)draft[ownerRole]=chosen;else delete draft[ownerRole];ports.setDraft(draft);}
      if(!disposed&&runId===ownerRun&&(ports.current()?.run_id||'')===ownerRun)status.textContent=ports.copy('roleModelSaved');
    }catch(error){if(!disposed&&runId===ownerRun&&(ports.current()?.run_id||'')===ownerRun)status.textContent=error instanceof Error?error.message:String(error);}
    finally{saving=false;if(!disposed){save.disabled=false;select.disabled=false;}}
  }
  return {open(next:EvolutionRole){
    role=next;runId=ports.current()?.run_id||'';title.textContent=ports.copy(role);status.textContent='';
    task.hidden=!runId||role==='coordinator';human.hidden=!runId||role!=='executor';refreshOptions();
  },refreshOptions,sync(){
    if((ports.current()?.run_id||'')!==runId){runId=ports.current()?.run_id||'';status.textContent='';refreshOptions();
      task.hidden=!runId||role==='coordinator';human.hidden=!runId||role!=='executor';}
  },dispose(){disposed=true;}};
}
