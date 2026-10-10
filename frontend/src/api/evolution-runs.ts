/** Headless run transport. Wire authority: contracts/evolution_run_v1.schema.json. */
import { request } from './transport';
import type { EvolutionActivationIntent } from './evolution';
import type { ExplorationHypothesis } from './evolution-programs';
import type { ModelRoutingModelRef } from './api-v1-model-routing.generated';
export type EvolutionModel = ModelRoutingModelRef | {provider_id: string; offering_id: string};
export type EvolutionStageTerminalVerdict = 'completed'|'interrupted'|'truncated'|'failed';
export type EvolutionRole = 'executor'|'reviewer'|'coordinator';
export type RoleModels = Partial<Record<EvolutionRole, EvolutionModel>>;

export interface RunBudget {max_tokens?:number|null;max_wall_clock_seconds?:number|null;max_tool_calls?:number|null}
export interface ReviewReserve {tokens?:number;tool_calls?:number;wall_clock_seconds?:number}
export type EvolutionSourceKind = 'deliverable' | 'tool_evidence' | 'artifact_evidence' | 'background_evidence';
export interface EvolutionSourceReference {task_id: string; source_sha256: string; total_chars: number; source_kind?: EvolutionSourceKind; complete?: boolean}
export interface HypothesisAxisAssessment {outcome: 'pass' | 'fail' | 'unknown'; evidence_ref: string}
export interface HypothesisAssessment {hypothesis_sha256: string; problem: HypothesisAxisAssessment;
  feasibility: HypothesisAxisAssessment; novelty: HypothesisAxisAssessment; net_value: HypothesisAxisAssessment}
export interface EvolutionRun {
  exploration_hypothesis?: ExplorationHypothesis;
  result_source?: EvolutionSourceReference;
  tool_evidence_source?: EvolutionSourceReference;
  artifact_evidence_source?: EvolutionSourceReference;
  background_evidence_source?: EvolutionSourceReference;
  background_wait?: boolean;
  review_reserve?: ReviewReserve;
  run_id: string; objective: string; status: string; task_id: string;
  attempt: number; model?: EvolutionModel; role_models?: RoleModels; created_at: number; updated_at: number;
  domain?: 'general' | 'research';

  budget?: RunBudget | null;
  actuals: {tokens: number; tool_calls: number; wall_clock_seconds: number};
  actuals_complete?: boolean;
  max_review_recoveries?: number;
  max_review_format_retries?: number;
  stage_losses?: Array<{task_id: string; stage: 'executing' | 'reviewing'; episode_id: string; admitted_recovery: boolean}>;
  result: string; error: string;
  acceptance_requirements?: string[];
  require_requirement_originals?: true;
  project_path?: string;
  stage_launch?: {intent_id: string; stage: 'executing' | 'reviewing'};
  stage_dispatch?: {task_id: string; intent_id: string; birth_version: number;
    state: 'bound' | 'admitted' | 'refused';
    prepared_input?: {task_id: string; sha256: string}; replaces_task_id?: string};
  learning?: {complete?: boolean; steps?: {refetch?: {result?: {deferred?: boolean}}}};
  review: {evidence_audit?: {complete: boolean; objections: Array<{criterion_id: 'objective' | 'evidence' | 'delivery'; requirement_id?: string; classification: 'contradicted' | 'unverified'; claim: string; evidence_ref: string}>}; hypothesis_assessment?: HypothesisAssessment; outcome: string; criterion_results: Array<{criterion_id: string; outcome: string; evidence_ref: string}>;
    requirement_results?: Array<{requirement_id: string; outcome: 'pass' | 'fail' | 'unknown'; evidence_ref: string; original_sources?: EvolutionSourceReference[]}>} | null;
  receipts: Array<{task_id: string; stage: string; status?: string;
    review_format_normalization?: {source: EvolutionSourceReference; json_offset: number; json_end: number; wrapped_json: boolean; field_paths: string[]};
    review_format_retry?: {error: 'invalid_json' | 'text_limit' | 'originals_uninspected'; admitted: boolean; intent_id: string; refusal: '' | 'retry_budget' | 'execution_budget' | 'frozen_evidence_unavailable'};
    actuals: EvolutionRun['actuals']; actuals_complete?: boolean; model?: EvolutionModel;
    submission_voided?: true; outcome_available?: boolean; restored_from_outcome?: boolean; terminal_verdict?: EvolutionStageTerminalVerdict}>;
}
export interface EvolutionStageOutcomePage {
  source_kind?: EvolutionSourceKind; tool_evidence_available?: boolean;
  tool_evidence_scope?: 'terminal_retained_tool_messages' | 'terminal_retained_tool_messages_and_background_completions' | 'terminal_retained_tool_messages_and_scientific_command_originals' | 'terminal_retained_tool_messages_and_background_completions_and_scientific_command_originals';
  background_evidence_available?: boolean; background_evidence_scope?: 'owned_background_command_completions';
  artifact_evidence_available?: boolean; artifact_evidence_complete?: boolean;
  artifact_evidence_scope?: 'declared_project_artifact_snapshots';
  task_id: string; stage: 'executing'|'reviewing'; status: 'done'|'error'|'aborted'|'interrupted';
  content: string; offset: number; total_chars: number; next_offset: number|null;
  archive_state: 'collecting'|'sealed'; terminal_verdict: EvolutionStageTerminalVerdict; source_sha256: string;
  actuals: EvolutionRun['actuals']; actuals_complete: boolean;
}
export interface ResearchRunParams {lang?: 'zh'|'en'; n_ideas?: number; seed_arxiv_ids?: string[]; harvest_n?: number; model?: string}
export interface CreateRunBody {objective: string; model?: EvolutionModel; role_models?: RoleModels; domain?: 'general'|'research'; research?: ResearchRunParams;
  acceptance_requirements?: string[]; require_requirement_originals?: true; project_path?: string; budget?: RunBudget | null; review_reserve?: ReviewReserve; max_attempts?: number | null; max_review_recoveries?: number; max_review_format_retries?: number}
export type EvolutionRunSummary = Pick<EvolutionRun, 'run_id' | 'objective' | 'status' | 'task_id' | 'attempt' | 'created_at' | 'updated_at'>;
export interface EvolutionFloorItem {
  kind: 'evolution' | 'flow' | 'goal';
  run_id: string; title: string; status: string; updated_at: number; conversation_id: string;
}
export interface EvolutionFloor {items: EvolutionFloorItem[]; degraded: boolean}
export interface EvolutionProposalItem {
  mutation_id: string; kind: string; status: string; version: number;
  title: string; evidence_count: number; updated_at: number;
  activation_intent?: EvolutionActivationIntent;
}
export interface EvolutionGatePending {items: EvolutionProposalItem[]; degraded: boolean}
export interface EvolutionGateResult {
  mutation: unknown; final_status: 'active' | 'regression_failed' | 'needs_waiver' | string;
  receipt?: Record<string, unknown>;
}
export interface HumanQuestion {
  guidance_id: string; question: string; response_type: string; options: string[];
  questions: Array<{question: string; response_type: string; options?: string[]}>;
}
export interface GoalMaterial {
  material_id:string;run_id:string;kind:'paper'|'repository'|'web'|'dataset'|'other';
  title:string;url:string;note:string;origin:'user'|'agent';task_id:string;created_at:number;updated_at:number;
}
export type MaterialInput = Pick<GoalMaterial,'kind'|'title'|'url'|'note'>;
export interface GoalWorkspace {materials:GoalMaterial[];stages:Array<{kind:string;detail:string}>;criteria:Array<{criterion_id:string;requirement:string}>;budget?:RunBudget|null;}
export interface GoalUpdate {
  seq:number;event_id:string;kind:string;content:string;references:string[];
  origin:'user'|'agent'|'system';task_id:string;target:string;created_at:number;
  source_seq?:number;detail_required?:boolean;
}
export interface GoalCoordination {
  updates:GoalUpdate[];revision:number;through_seq:number;has_more:boolean;has_earlier:boolean;before_seq:number;
  delivery:Record<string,{status:'saved'|'considered'|'assigned'|'accepted'|'not_applicable'|'conflict';note:string;assignment_seq?:number}>;
  coordinator:{enabled:boolean;status:string;task_id:string;last_response:string;error:string};
  followup?:{status:string;run_id:string}|null;
}
export interface SwarmAgentStatus {id:string;role:string;status:string;objective?:string}
export interface SwarmStatus {active:boolean|null;known?:boolean;agents?:SwarmAgentStatus[]}
const base = '/api/v4/evolution';
const runPath = (id: string) => `${base}/runs/${encodeURIComponent(id)}`;
export const evolutionRunsApi = {
  listRuns: (signal?: AbortSignal) => request<{runs: EvolutionRunSummary[]; floor?: EvolutionFloor; proposals?: EvolutionGatePending}>(`${base}/runs`, {signal}),
  getRun: (id: string, signal?: AbortSignal) => request<{run: EvolutionRun; pending_questions: HumanQuestion[];workspace?:GoalWorkspace}>(runPath(id), {signal}),
  createRun: (body: CreateRunBody, key: string) => request<{run: EvolutionRun}>(`${base}/runs`, {method:'POST', json:body, headers:{'Idempotency-Key':key}}),
  getStageOutcome: (id: string, taskId: string, offset=0, signal?: AbortSignal, sourceKind?: EvolutionSourceKind) =>
    request<{outcome: EvolutionStageOutcomePage}>(`${runPath(id)}/outcomes/${encodeURIComponent(taskId)}`,
      {query: {offset, ...(sourceKind && sourceKind !== 'deliverable' ? {source_kind: sourceKind} : {})}, signal}),
  updateRoleModel: (id:string, role:EvolutionRole, model:EvolutionModel|null) => request<{run:EvolutionRun}>(`${runPath(id)}/role-model`, {method:'POST',json:{role,model}}),
  cancelRun: (id: string) => request<{run: EvolutionRun}>(`${runPath(id)}/cancel`, {method:'POST',json:{}}),
  reconcileRun: (id: string) => request<{run: EvolutionRun}>(`${runPath(id)}/reconcile`, {method:'POST',json:{}}),
  answerRun: (id: string, guidance_id: string, response: string) => request<{run: EvolutionRun}>(`${runPath(id)}/answers`, {method:'POST',json:{guidance_id,response}}),
  saveMaterial:(id:string,body:MaterialInput)=>request<{material:GoalMaterial}>(`${runPath(id)}/materials`,{method:'POST',json:body}),
  removeMaterial:(id:string,materialId:string)=>request<{removed:string}>(`${runPath(id)}/materials/${encodeURIComponent(materialId)}`,{method:'DELETE'}),
  getCoordination:(id:string,signal?:AbortSignal,before?:number)=>request<GoalCoordination>(`${runPath(id)}/coordination?${before?`before=${before}`:'view=recent'}`,{signal}),
  sendGoalMessage:(id:string,content:string,key:string)=>request<{update:GoalUpdate;coordinator:{status:string;error?:string}}>(`${runPath(id)}/updates`,{method:'POST',json:{kind:'message',content},headers:{'Idempotency-Key':key}}),
  getGoalUpdate:(id:string,seq:number,signal?:AbortSignal)=>request<{update:GoalUpdate}>(`${runPath(id)}/updates/${seq}`,{signal}),
  reconcileCoordinator:(id:string)=>request<{coordinator:GoalCoordination['coordinator']}>(`${runPath(id)}/coordinator/reconcile`,{method:'POST',json:{}}),
  followupRun:(id:string,content:string,key:string)=>request<{run:EvolutionRun}>(`${runPath(id)}/followups`,{method:'POST',json:{kind:'guidance',content},headers:{'Idempotency-Key':key}}),
  listRunModels: (signal?: AbortSignal) => request<{models: Array<{model: EvolutionModel; label: string}>}>(`${base}/models`, {signal}),
  getSwarmStatus: (taskId: string, signal?: AbortSignal) => request<SwarmStatus>(`/api/v4/swarm/status/${encodeURIComponent(taskId)}`, {signal}),
};
