/** Independently mountable Evolution page. API/navigation are injected; no chat DOM.
 * User forms and evidence disclosures remain stable while controller data refreshes.
 */
import type { EvolutionApi, EvolutionState } from './controller';
import { createEvolutionController, emptyState } from './controller';
import { overview, goals, goalDetail, capabilities, mutationDetail, liveEpisode, floor } from './components/presentation';
import { goalForm, teachForm, assessmentForm } from './components/forms';
import { button, copy, esc, patch } from './components/ui';
import { projectActivity, type EpisodeActivity } from './components/activity';

export function mountEvolution(root: HTMLElement, ports: {
  api: EvolutionApi; path: string; navigate: (path: string) => void;
  openTask: (id: string) => void;
}) {
  let state: EvolutionState = emptyState();
  let path = ports.path;
  let disposed = false;
  let loaded = false;
  let busy = false;
  let signature = '';
  let formPath = '';
  root.classList.add('evo-workspace');
  root.innerHTML = `<nav class="evo-nav" aria-label="${esc(copy('navigation'))}"><a href="#/evolution" data-tab="overview">${esc(copy('overview'))}</a><a href="#/evolution/goals" data-tab="goals">${esc(copy('goals'))}</a><a href="#/evolution/floor" data-tab="floor">${esc(copy('floor'))}</a><a href="#/evolution/research" data-tab="research">${esc(copy('research'))}</a><a href="#/evolution/capabilities" data-tab="capabilities">${esc(copy('capabilities'))}</a></nav><div class="evo-main"><header class="evo-page-head"><div><h1 tabindex="-1">${esc(copy('title'))}</h1><p>${esc(copy('subtitle'))}</p></div><div class="evo-actions">${button('refresh',copy('refresh'))}${button('new',copy('newGoal'),'',true)}</div></header><div class="evo-error" role="alert" hidden></div><div class="evo-content" aria-live="off"></div><div class="evo-form-host" hidden></div></div>`;
  const content = root.querySelector<HTMLElement>('.evo-content')!;
  const formHost = root.querySelector<HTMLElement>('.evo-form-host')!;
  const error = root.querySelector<HTMLElement>('[role=alert]')!;
  const showError = (message: string) => { if(disposed)return; error.textContent=message; error.hidden=!message; };

  /* Live activity projection for running episodes. Keyed by episode_id; only
   * refreshed while a `live/<id>` or `floor` route is on screen, and only for
   * episodes still reported as running by the authoritative controller. */
  const activities = new Map<string, EpisodeActivity>();
  let liveTimer = 0;
  let liveFlight: Promise<void> | undefined;
  const runningEpisodes = () => state.episodes.filter(e=>e.status==='running');
  const isLiveRoute = () => path.split('/')[1] === 'live';
  const isFloorRoute = () => path.split('/')[1] === 'floor';
  const liveWanted = () => !disposed && !document.hidden && (isLiveRoute() || isFloorRoute());
  async function pollLive(): Promise<void> {
    if (!liveWanted() || liveFlight) return;
    const running = runningEpisodes();
    if (!running.length) { stopLive(); return; }
    liveFlight = (async () => {
      await Promise.all(running.map(async e=>{
        try {
          const fold = e.task_id && ports.api.taskRequests
            ? await ports.api.taskRequests(e.task_id) : null;
          const requests = fold && Array.isArray(fold.requests) ? fold.requests : [];
          activities.set(e.episode_id, projectActivity(e.stage_receipts, requests, true));
        } catch { /* keep last projection; a transient read must not blank the live view */ }
      }));
      if (!disposed) { signature=''; render(); }
    })().finally(() => { liveFlight = undefined; });
    return liveFlight;
  }
  function scheduleLive(): void {
    if (liveTimer || !liveWanted()) return;
    liveTimer = window.setTimeout(() => { liveTimer = 0; void pollLive().finally(scheduleLive); }, 2500);
  }
  function stopLive(): void { if (liveTimer) window.clearTimeout(liveTimer); liveTimer = 0; }
  function syncLive(): void {
    if (liveWanted() && runningEpisodes().length) { void pollLive(); scheduleLive(); }
    else stopLive();
  }
  function render(): void {
    if (disposed || busy) return;
    const [, tab = '', encodedId = ''] = path.split('/');
    let id = ''; try { id = decodeURIComponent(encodedId); } catch { showError(copy('notFound')); }
    const form = tab === 'new' || tab === 'teach';
    formHost.hidden = !form; content.hidden = form;
    root.querySelectorAll<HTMLAnchorElement>('[data-tab]').forEach(a=>{
      const current = tab==='goals' ? 'goals' : tab==='floor'||tab==='live' ? 'floor' : ['capabilities','teach'].includes(tab) ? 'capabilities':'overview';
      const selected = a.dataset.tab === current;
      if(selected)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');
    });
    if(form) {
      if(formPath===path)return;
      if(tab==='new') goalForm(formHost,ports.api,goalId=>{ if(disposed)return; formPath=''; formHost.replaceChildren(); void controller.refreshAfterWrite().then(()=>{if(!disposed)ports.navigate(`evolution/goals/${encodeURIComponent(goalId)}`);}); });
      else {
        const gap=state.gaps.find(g=>g.gap_id===id);
        if(!gap){formHost.textContent=copy(loaded?'notFound':'loading');return;}
        teachForm(formHost,ports.api,gap,()=>{if(!disposed)void controller.refreshAfterWrite();});
      }
      formPath=path; return;
    }
    if(!loaded){content.textContent=copy('loading');return;}
    const liveState = tab==='live'||tab==='floor' ? [...activities.entries()] : [];
    const next=JSON.stringify([path,state,liveState]); if(next===signature)return; signature=next;
    const bubbles: Record<string,string> = {};
    if (tab==='floor') for (const [ep,act] of activities) bubbles[ep]=act.stage;
    const html = tab==='live' ? liveEpisode(state,id,activities.get(id))
      : tab==='floor' ? floor(state,bubbles)
      : tab==='goals' ? (id?goalDetail(state,id):goals(state))
      : tab==='capabilities' ? (id?mutationDetail(state,id):capabilities(state))
      : overview(state);
    patch(content,html);
    syncLive();
  }
  const controller=createEvolutionController(ports.api,{
    update:(next,errors)=>{state=next;loaded=true;if(errors.length)showError(`${copy('partialLoad')} ${errors.join(' · ')}`);render();},
    visible:()=>!document.hidden,
    schedule:(fn,delay)=>window.setTimeout(fn,delay),cancel:id=>window.clearTimeout(id),
  });
  const refresh=async()=>{await controller.refreshAfterWrite();};
  const visibility=()=>controller.visibilityChanged();
  document.addEventListener('visibilitychange',visibility);
  const onClick=async(event:Event)=>{
    const node=(event.target as Element).closest<HTMLButtonElement>('[data-evo-action]');
    if(!node || !root.contains(node) || node.disabled)return;
    const action=node.dataset.evoAction, id=node.dataset.id || '';
    if(action==='new'){ports.navigate('evolution/new');return;}
    if(action==='task'){ports.openTask(id);return;}
    if(action==='openLive'){ports.navigate(`evolution/live/${encodeURIComponent(id)}`);return;}
    if(busy)return;
    busy=true;node.disabled=true;showError('');
    try {
      if(action==='refresh')await refresh();
      if(action==='propose'){await ports.api.proposePlan(id);await refresh();}
      if(action==='launch'){
        const plan=state.plans.find(p=>p.plan_id===id);
        if(plan?.status==='proposed') await ports.api.confirmPlan(id);
        // Refresh in finally even if launch fails: a confirmed plan remains retryable.
        try {await ports.api.launchPlan(id);} finally {await refresh();}
      }
      if(action==='rejectPlan'){await ports.api.rejectPlan(id);await refresh();}
      if(action==='rejectMutation'||action==='rollback'){
        await ports.api.mutationAction(id,action==='rollback'?'rollback':'reject');await refresh();
      }
      if(action==='enable'||action==='waiver'){
        let result;
        try {result=await ports.api.mutationAction(id,action==='waiver'?'enable_waiver':'enable');}
        catch(reason){await refresh();throw reason;}
        if(disposed)return;
        const island=content.querySelector<HTMLElement>('.evo-gate-result');
        if(island){
          island.textContent=copy(result.final_status==='needs_waiver'?'waiverHint':result.final_status==='regression_failed'?'regressionFailed':result.final_status==='active'?'enabled':'unknown');
          if(result.receipt){const receipt=document.createElement('pre');receipt.textContent=JSON.stringify(result.receipt,null,2);island.appendChild(receipt);}
          if(result.final_status==='needs_waiver')island.insertAdjacentHTML('beforeend',button('waiver',copy('waiver'),id));
        }
        await refresh();
      }
      if(action==='assess'){
        const episode=state.episodes.find(e=>e.episode_id===id);
        if(episode){
          const task=await ports.api.taskStatus(episode.task_id);
          if(disposed)return;
          const island=[...content.querySelectorAll<HTMLElement>('.evo-assessment')].find(n=>n.dataset.key===`assess-${id}`);
          if(island){
            if(['done','error','aborted'].includes(task.status))assessmentForm(island,ports.api,episode,state.goals.find(g=>g.goal_id===episode.goal_id),()=>{if(!disposed)void refresh();});
            else island.textContent=copy('stillRunning');
          }
        }
      }
      if(action==='report'){
        const island=content.querySelector<HTMLElement>('.evo-report');
        if(island){island.hidden=false;island.textContent=copy('loading');
          const result=await ports.api.goalReport(id);if(!disposed)island.textContent=result.report||copy('noEvidence');}
      }
    } catch(reason){showError(reason instanceof Error?reason.message:String(reason));}
    finally{busy=false;node.disabled=false;if(!disposed){signature='';render();}}
  };
  root.addEventListener('click',onClick);
  render();void controller.refresh();
  return {
    updateRoute(next:string){ if(next===path)return;path=next;signature='';render();syncLive();root.querySelector<HTMLElement>('h1')?.focus(); },
    dispose(){disposed=true;stopLive();controller.dispose();document.removeEventListener('visibilitychange',visibility);root.removeEventListener('click',onClick);root.replaceChildren();},
  };
}
