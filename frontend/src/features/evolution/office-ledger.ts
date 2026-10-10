/** Compact, read-only office board. Every line projects the existing run,
 * workspace, coordinator and swarm snapshots; it owns no polling or state.
 * Missing snapshots display an unknown mark rather than an invented zero.
 */
import type { EvolutionRun, GoalWorkspace, GoalCoordination, SwarmAgentStatus } from '../../api/evolution-runs';

import { failureReason, runStatusLabel, runTelemetry } from './goal-workbench';

type Snapshot = {
  run?: EvolutionRun;
  workspace?: GoalWorkspace;
  coordination?: GoalCoordination;
  coordinationError?: string;
  agents: SwarmAgentStatus[];
  agentsKnown: boolean;
  questions: number;
};

export function mountOfficeLedger(host:HTMLElement, copy:(key:string)=>string) {
  host.innerHTML=`<div class="evo-ledger-brand" aria-hidden="true"><b>Tofu</b><small>让 AI 研究发生</small></div>
    <button type="button" class="evo-ledger-head" data-studio-action="details"><span></span><strong></strong></button>
    <div class="evo-ledger-roles"></div>
    <footer class="evo-ledger-facts"><span data-fact="materials"></span><span data-fact="agents"></span><span data-fact="usage"></span></footer>
    <p class="evo-ledger-error" hidden></p>`;
  host.setAttribute('aria-label',copy('currentTask'));
  const headStatus=host.querySelector<HTMLElement>('.evo-ledger-head span')!;
  const headTitle=host.querySelector<HTMLElement>('.evo-ledger-head strong')!;
  const roles=host.querySelector<HTMLElement>('.evo-ledger-roles')!;
  const rows=['executor','reviewer','coordinator'].map((key,index)=>{
    const row=document.createElement('div');row.className='evo-ledger-role';row.dataset.role=key;
    row.innerHTML=`<i aria-hidden="true"></i><span></span><strong></strong>`;
    row.querySelector('span')!.textContent=copy(key);
    roles.append(row);
    return {index,row,state:row.querySelector('strong')!};
  });
  const materials=host.querySelector<HTMLElement>('[data-fact=materials]')!;
  const agents=host.querySelector<HTMLElement>('[data-fact=agents]')!;
  const usage=host.querySelector<HTMLElement>('[data-fact=usage]')!;
  const error=host.querySelector<HTMLElement>('.evo-ledger-error')!;
  function update(snapshot:Snapshot){
    const {run,workspace,coordination}=snapshot;
    host.hidden=false;
    if(!run){
      headStatus.textContent=copy('studio');headTitle.textContent=copy('roomNoGoal');
      rows.forEach(({row,state})=>{row.dataset.status='idle';state.textContent=copy('idle');});
      materials.textContent='';agents.textContent='';usage.textContent='';error.hidden=true;
      return;
    }
    headStatus.textContent=runStatusLabel(run.status,copy);
    headStatus.dataset.status=run.status;
    headTitle.textContent=run.objective;
    headTitle.title=run.objective;
    const executing=run.status==='executing';
    const reviewing=run.status==='reviewing';
    const executionReceipt=run.receipts.some(item=>item.stage==='executing');
    const reviewReceipt=run.receipts.some(item=>item.stage==='reviewing');
    const statuses=[
      executing?(snapshot.questions?'askHuman':'working'):executionReceipt?'recorded':'idle',
      reviewing?(snapshot.questions?'askHuman':'reviewing'):run.review?(run.review.outcome==='success'?'completed':'needs_attention'):reviewReceipt?'recorded':'idle',
      coordination?.coordinator.status==='running'?'coordinatorThinking'
        :coordination?.coordinator.status==='needs_attention'?'coordinatorBlocked'
        :coordination?.coordinator.status==='idle'?'idle':'',
    ];
    rows.forEach(({index,row,state})=>{
      const value=statuses[index];
      row.dataset.status=value||'unknown';
      state.textContent=value?copy(value):'—';
    });
    materials.textContent=`${workspace?workspace.materials.length:'—'} · ${copy('place_library')}`;
    agents.textContent=`${snapshot.agentsKnown?snapshot.agents.length:'—'} · ${copy('agentRoomAgent')}`;
    const accounted=run.actuals.tokens||['completed','failed','needs_attention','cancelled'].includes(run.status)
      ?`${run.actuals_complete===false?'≥ ':''}${run.actuals.tokens.toLocaleString()}`:'—';
    const telemetry=runTelemetry(run,workspace,copy);
    usage.textContent=`${accounted} tokens · ${telemetry.tokenLimit}`;
    error.textContent=failureReason(run.error||coordination?.coordinator.error,copy)||(snapshot.coordinationError?copy('coordinatorBlocked'):'');
    error.hidden=!error.textContent;
    error.title=error.textContent;
  }
  return {update};
}
