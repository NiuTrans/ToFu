/** Pure game-screen projection. One function maps confirmed run, goal workspace,
 * coordinator, question and swarm snapshots to everything the studio presents:
 * phase, wait, stage track, actor intents, meters, counts and the one primary
 * action. A second pure function turns two consecutive views of the same goal
 * into discrete game events. No DOM, copy, timers or transport.
 * Entries: projectStudio, studioEvents, failureCode, statusKey, actorStatusKey.
 * Wire authority: contracts/evolution_run_v1.schema.json.
 */
import type { EvolutionRun, GoalWorkspace, GoalCoordination, SwarmAgentStatus } from '../../../api/evolution-runs';

export type StudioPhase = 'none'|'loading'|'preparing'|'executing'|'reviewing'|'settling'
  |'completed'|'failed'|'attention'|'cancelled'|'unknown';
export type StudioWait =
  | {kind:'human';count:number}
  /** `error` is set while the job's status cannot be read or verified; `routed`
   * means the owner's Router Watchdog holds an open signal for it. */
  | {kind:'compute';operation:string;error:string;origin:string;since:number|null;routed:boolean}
  | {kind:'background'};
export type StageId = 'plan'|'execute'|'review'|'deliver';
export type StageState = 'pending'|'active'|'waiting'|'done'|'failed'|'stopped';
export type ActorRole = 'executor'|'reviewer'|'coordinator';
export type ActorMode = 'idle'|'working'|'waiting'|'blocked'|'done'|'failed';
export type Emote = ''|'work'|'think'|'ask'|'compute'|'hourglass'|'alert'|'fail';
export type Verdict = 'pass'|'fail'|'partial'|'unverified';
export type FailureCode = 'limit'|'usage'|'unavailable'|'interrupted'|'admission'|'recovery'
  |'unverified'|'intake'|'delivery'|'guidance'|'research'|'stopped'|'unknown';
export interface ActorView {role:ActorRole;mode:ActorMode;emote:Emote;taskId:string}
export interface Meter {value:number;lowerBound:boolean;limit:number|null}
export interface StudioView {
  phase:StudioPhase;
  runId:string;
  objective:string;
  terminal:boolean;
  wait?:StudioWait;
  verdict?:Verdict;
  failure:FailureCode|'';
  /** Raw backend error code, for the precise long explanation on demand. */
  error:string;
  stages:Record<StageId,StageState>;
  actors:Record<ActorRole,ActorView>;
  meters?:{tokens:Meter;calls:Meter;seconds:Meter};
  counts:{materials:number|null;steps:number|null;criteria:number|null;records:number};
  deliverable:boolean;
  questions:number;
  coordinator:'idle'|'thinking'|'attention';
  agents:{known:boolean;live:number};
  primary:'answer'|'stall'|'read'|'retry'|'create'|'';
}
export interface StudioSnapshot {
  run?:EvolutionRun;
  workspace?:GoalWorkspace;
  coordination?:GoalCoordination;
  questions?:number;
  loading?:boolean;
  agents?:SwarmAgentStatus[];
  agentsKnown?:boolean;
}
export const STAGES:StageId[] = ['plan','execute','review','deliver'];

const PHASES:Record<string,StudioPhase> = {
  created:'preparing', planning:'preparing', preparing:'preparing',
  executing:'executing', reviewing:'reviewing', settling:'settling',
  completed:'completed', failed:'failed', needs_attention:'attention', cancelled:'cancelled',
};
const TERMINAL = new Set<StudioPhase>(['completed','failed','attention','cancelled']);
const STOPPED = new Set<StudioPhase>(['failed','attention','cancelled']);
const VERDICTS:Record<string,Verdict> = {
  success:'pass', pass:'pass', failure:'fail', failed:'fail', fail:'fail',
  partial:'partial', inconclusive:'unverified', unmeasurable:'unverified',
};
const FAILURES:Record<string,FailureCode> = {
  execution_token_usage_unknown:'usage',
  task_unavailable:'unavailable',
  stage_handoff_interrupted:'interrupted',
  stage_admission_deferred:'admission', review_worker_lost:'admission',
  stage_launch_recovery_refused:'recovery', stage_loss_recovery_refused:'recovery',
  stage_outcome_recovery_refused:'recovery', pending_stage_restoration_refused:'recovery',
  task_terminal_delivery_unverified:'unverified',
  intake_failed:'intake',
  delivery_empty_or_exceeds_inline_limit:'delivery',
  unacknowledged_goal_guidance:'guidance',
  research_record_unavailable_or_not_terminal:'research',
  cancelled:'stopped', stopped:'stopped',
};

/** Stable failure family of a backend error code; unknown codes stay 'unknown'. */
export function failureCode(error:string|undefined):FailureCode|'' {
  if(!error)return '';
  const code=error.split(/[;:]/,1)[0].trim().toLowerCase();
  if(FAILURES[code])return FAILURES[code];
  if(/budget|token_limit|max_tokens|tool_call_limit|wall_clock|timeout|time_limit/.test(code))return 'limit';
  return 'unknown';
}

function meter(value:number|undefined,limit:number|null|undefined,lowerBound:boolean):Meter {
  const measured=typeof value==='number'&&Number.isFinite(value)?Math.max(0,value):0;
  const cap=typeof limit==='number'&&Number.isFinite(limit)&&limit>0?limit:null;
  return {value:measured,lowerBound,limit:cap};
}

function waitOf(run:EvolutionRun,questions:number):StudioWait|undefined {
  if(questions>0)return {kind:'human',count:questions};
  const compute=run.compute_wait;
  if(compute)return {kind:'compute',operation:compute.operation_id,error:compute.last_error||'',
    origin:compute.last_error_origin||'',since:compute.failed_since??null,routed:!!compute.watchdog_signal_id};
  if(run.background_wait)return {kind:'background'};
  return undefined;
}

const IDLE = (role:ActorRole,taskId=''):ActorView => ({role,mode:'idle',emote:'',taskId});

export function projectStudio(snapshot:StudioSnapshot):StudioView {
  const {run,workspace,coordination}=snapshot;
  const questions=Math.max(0,snapshot.questions||0);
  const coordinator:StudioView['coordinator']=coordination?.coordinator.status==='running'?'thinking'
    :coordination?.coordinator.status==='needs_attention'?'attention':'idle';
  const live=(snapshot.agents||[]).filter(agent=>agent.status==='running'||agent.status==='pending').length;
  const base={
    runId:'',objective:'',terminal:false,failure:'' as const,error:'',deliverable:false,questions,coordinator,
    agents:{known:snapshot.agentsKnown!==false,live},
    counts:{materials:null,steps:null,criteria:null,records:0},
    stages:{plan:'pending',execute:'pending',review:'pending',deliver:'pending'} as Record<StageId,StageState>,
    actors:{executor:IDLE('executor'),reviewer:IDLE('reviewer'),coordinator:coordinatorActor(coordination)},
  };
  if(!run)return {...base,phase:snapshot.loading?'loading':'none',primary:snapshot.loading?'':'create'};

  const phase=PHASES[run.status]||'unknown';
  const terminal=TERMINAL.has(phase);
  const wait=terminal?undefined:waitOf(run,questions);
  const verdict=run.review?VERDICTS[run.review.outcome]||'unverified':undefined;
  const receipts=(run.receipts||[]).filter(receipt=>receipt.submission_voided!==true);
  const executions=receipts.filter(receipt=>receipt.stage==='executing');
  const reviews=receipts.filter(receipt=>receipt.stage==='reviewing');
  const lastExecution=executions.at(-1),lastReview=reviews.at(-1);
  const executionFailed=!!lastExecution?.terminal_verdict&&lastExecution.terminal_verdict!=='completed';
  // A compute park writes a placeholder result; it is never a deliverable.
  const deliverable=!!run.result&&!run.compute_wait;

  const stages:Record<StageId,StageState>={
    plan:phase==='preparing'?'active':'done',
    execute:phase==='executing'?(wait?'waiting':'active'):executionFailed?'failed':lastExecution?'done':'pending',
    review:phase==='reviewing'?(wait?'waiting':'active'):verdict?(verdict==='fail'?'failed':'done'):'pending',
    deliver:phase==='settling'?'active':phase==='completed'?'done':'pending',
  };
  if(phase==='completed')for(const id of STAGES)if(stages[id]==='pending')stages[id]='done';
  if(STOPPED.has(phase)){
    // The pipeline stopped at its first unfinished stage.
    const at=STAGES.find(id=>stages[id]!=='done');
    if(at&&stages[at]!=='failed')stages[at]=phase==='cancelled'?'stopped':'failed';
  }

  const executor:ActorView={role:'executor',mode:'idle',emote:'',
    taskId:phase==='executing'?run.task_id:lastExecution?.task_id||''};
  if(phase==='executing'){
    if(wait?.kind==='human')Object.assign(executor,{mode:'waiting',emote:'ask'});
    else if(wait?.kind==='compute')Object.assign(executor,{mode:'waiting',emote:wait.error?'alert':'compute'});
    else if(wait?.kind==='background')Object.assign(executor,{mode:'waiting',emote:'hourglass'});
    else Object.assign(executor,{mode:'working',emote:'work'});
  }else if(executionFailed&&STOPPED.has(phase)&&!reviews.length)Object.assign(executor,{mode:'failed',emote:'fail'});
  else if(lastExecution)executor.mode='done';

  const reviewer:ActorView={role:'reviewer',mode:'idle',emote:'',
    taskId:phase==='reviewing'?run.task_id:lastReview?.task_id||''};
  if(phase==='reviewing'){
    if(wait?.kind==='human')Object.assign(reviewer,{mode:'waiting',emote:'ask'});
    else Object.assign(reviewer,{mode:'working',emote:'work'});
  }else if(verdict||lastReview)reviewer.mode='done';

  // A running stage's usage reaches the run only at settlement: until then the
  // recorded total is a lower bound, never the live consumption.
  const running=phase==='reviewing'||(phase==='executing'&&(!wait||wait.kind==='human'));
  const lowerBound=run.actuals_complete===false||running;
  const budget=workspace?.budget??run.budget;
  const failure=STOPPED.has(phase)?failureCode(run.error||(phase==='cancelled'?'cancelled':''))||(phase==='failed'?'unknown':''):'';
  return {
    ...base,phase,runId:run.run_id,objective:run.objective,terminal,wait,verdict,failure,error:run.error||'',deliverable,stages,
    actors:{executor,reviewer,coordinator:base.actors.coordinator},
    meters:{
      tokens:meter(run.actuals?.tokens,budget?.max_tokens,lowerBound),
      calls:meter(run.actuals?.tool_calls,budget?.max_tool_calls,lowerBound),
      seconds:meter(run.actuals?.wall_clock_seconds,budget?.max_wall_clock_seconds,lowerBound),
    },
    counts:{
      materials:workspace?workspace.materials.length:null,
      steps:workspace?workspace.stages.length:null,
      criteria:workspace?workspace.criteria.length:null,
      records:new Set([...receipts.map(receipt=>receipt.task_id),run.task_id].filter(Boolean)).size,
    },
    primary:questions?'answer':wait?.kind==='compute'&&wait.error?'stall'
      :terminal&&deliverable&&phase==='completed'?'read':STOPPED.has(phase)?'retry':'',
  };
}

function coordinatorActor(coordination:GoalCoordination|undefined):ActorView {
  const status=coordination?.coordinator.status;
  if(status==='running')return {role:'coordinator',mode:'working',emote:'think',taskId:coordination!.coordinator.task_id};
  if(status==='needs_attention')return {role:'coordinator',mode:'blocked',emote:'alert',taskId:coordination!.coordinator.task_id};
  return IDLE('coordinator',coordination?.coordinator.task_id||'');
}

/** Copy key for the one short status chip of the quest banner. */
export function statusKey(view:StudioView):string {
  if(view.wait?.kind==='human')return 'askHuman';
  if(view.wait?.kind==='compute')return view.wait.error?'computeStalled':'waitingCompute';
  if(view.wait?.kind==='background')return 'waitingBackground';
  if(view.phase==='completed'&&view.verdict&&view.verdict!=='pass')return 'verdict_'+view.verdict;
  return 'state_'+view.phase;
}

/** Copy key describing one actor's current mode. */
export function actorStatusKey(view:StudioView,role:ActorRole):string {
  const actor=view.actors[role];
  if(role==='coordinator')return actor.mode==='working'?'coordinatorThinking':actor.mode==='blocked'?'coordinatorBlocked':'mode_idle';
  if(actor.mode==='waiting'){
    if(view.wait?.kind==='human')return 'askHuman';
    if(view.wait?.kind==='compute')return view.wait.error?'computeStalled':'waitingCompute';
    return 'waitingBackground';
  }
  return 'mode_'+actor.mode;
}

export type StudioEvent =
  | {kind:'phase';phase:StudioPhase}
  | {kind:'wait';wait:StudioWait['kind']}
  | {kind:'computeError'}
  | {kind:'materials';added:number}
  | {kind:'question';count:number}
  | {kind:'verdict';verdict:Verdict};

/** Discrete transitions between two views of the same goal. A first view or a
 * goal switch yields nothing: loading a page is not an event. */
export function studioEvents(previous:StudioView|undefined,next:StudioView):StudioEvent[] {
  if(!previous||!next.runId||previous.runId!==next.runId)return [];
  const events:StudioEvent[]=[];
  if(previous.phase!==next.phase&&next.phase!=='loading')events.push({kind:'phase',phase:next.phase});
  if(next.wait&&next.wait.kind!=='human'&&previous.wait?.kind!==next.wait.kind)events.push({kind:'wait',wait:next.wait.kind});
  if(next.wait?.kind==='compute'&&next.wait.error&&!(previous.wait?.kind==='compute'&&previous.wait.error))events.push({kind:'computeError'});
  if(next.questions>previous.questions)events.push({kind:'question',count:next.questions});
  const before=previous.counts.materials,after=next.counts.materials;
  if(before!==null&&after!==null&&after>before)events.push({kind:'materials',added:after-before});
  if(next.verdict&&!previous.verdict)events.push({kind:'verdict',verdict:next.verdict});
  return events;
}
