/** Durable exploration transport. Authority: evolution_program_v1.schema.json. */
import { request } from './transport';
import type { CreateRunBody, EvolutionRun, RunBudget, EvolutionSourceReference } from './evolution-runs';

export interface ExplorationStep {
  title: string;
  kind: 'discovery' | 'baseline' | 'experiment' | 'synthesis' | 'replan' | 'final';
  instruction: string;
  checks: string[];
  hypothesis?: ExplorationHypothesis;
  recovery_of?: number;
}
export interface ReplanProposal {
  failed_sequence: number; source_run_id: string; request_id: string; request_digest: string;
  reason: string; steps: ExplorationStep[];
}
/** Frozen claim input; each reviewed axis retains its own uncertainty. */
export interface ExplorationHypothesis {
  problem: string; audience: string; baseline: string; scenario: string;
  mechanism: string; expected_value: string; falsifier: string;
  assumptions: string[]; evidence_refs: string[];
}
export type ProgramAdvanceMode = 'continuous' | 'checkpoint';
export interface CreateProgramBody {
  executor_schema_budget_tokens?: number;
  objective: string; research_source?: {task_id: string; idea_id: string}; acceptance?: string[]; steps?: ExplorationStep[];
  auto_replan?: boolean;
  advance_mode?: ProgramAdvanceMode;
  project_path?: string; max_attempts?: number | null; max_review_recoveries?: number; max_review_format_retries?: number;
  budget?: RunBudget | null; review_reserve?: CreateRunBody['review_reserve']; model?: CreateRunBody['model']; role_models?: CreateRunBody['role_models'];
}
export interface ExplorationProgram {
  final_evidence_version?: 1;
  schema_version: 1; program_id: string; objective: string; acceptance: string[];
  status: 'ready' | 'active' | 'paused' | 'needs_attention' | 'completed' | 'cancelled';
  queue: ExplorationStep[];
  auto_replan?: boolean;
  advance_mode?: ProgramAdvanceMode;
  started_at?: number | null;
  replan_proposal?: ReplanProposal;
  actuals_complete?: boolean;
  tool_calls_complete?: boolean;
  execution_options?: Pick<CreateProgramBody, 'executor_schema_budget_tokens' | 'project_path' | 'budget' | 'review_reserve' | 'model' | 'role_models' | 'max_attempts' | 'max_review_recoveries' | 'max_review_format_retries'>;
  current: {sequence: number; step: ExplorationStep; payload: CreateRunBody; context_seed: string; run_id: string; deadline_at: number | null; cancellation_accounting?: {actuals: EvolutionRun['actuals']; prior_complete: boolean}} | null;
  settled_through: number; actuals: EvolutionRun['actuals']; error: string;
  created_at: number; updated_at: number;
}
export interface ExplorationReceipt {
  result_source?: EvolutionSourceReference;
  tool_evidence_source?: EvolutionSourceReference;
  artifact_evidence_source?: EvolutionSourceReference;
  background_evidence_source?: EvolutionSourceReference;
  schema_version: 1; program_id: string; sequence: number; step: ExplorationStep;
  run_id: string; outcome: 'completed' | 'failed' | 'needs_attention' | 'cancelled';
  result: string; review: EvolutionRun['review']; actuals: EvolutionRun['actuals'];
  error: string; created_at: number;
  replan?: ReplanProposal;
  actuals_complete?: boolean;
}
export interface ExperimentPage {
  experiments: ExplorationReceipt[]; next_cursor: number | null; settled_through: number;
}
export interface ExplorationProgramSummary {
  program_id: string; objective: string; status: ExplorationProgram['status'];
  settled_through: number; pending_steps: number; current_title: string;
  created_at: number; updated_at: number;
}
export interface ProgramCatalogPage {
  programs: ExplorationProgramSummary[];
  next_cursor: string | null; previous_cursor: string | null; order: 'program_id_asc';
}
export interface ProgramCatalogQuery {after?: string; before?: string; limit?: number}
export interface SemanticMemoryEntry {
  key: string; query: string; created_at: number; model: string;
  source_kind?: 'receipt' | 'deliverable' | 'tool_evidence' | 'artifact_evidence' | 'background_evidence';
  actuals: EvolutionRun['actuals']; actuals_complete: boolean;
  source_chars: number; scanned_sources: number;
  source_chunks: Array<{candidate_id: string; sequence: number; offset: number; end: number; source_digest: string; source_kind?: 'deliverable' | 'tool_evidence' | 'artifact_evidence' | 'background_evidence'; run_id?: string; source_ref?: EvolutionSourceReference}>;
  matches: Array<{candidate_id: string; relevance: number; quote: string; reason: string}>;
  task_id: string; inspector_task_id: string;
}
export interface SemanticMemoryHistory {entries: SemanticMemoryEntry[]; retained_limit: number; evidence_status: string}
export type ProgramAction = 'start' | 'advance' | 'pause' | 'resume' | 'cancel' | 'retry';
const base = '/api/v4/evolution/programs';
const programPath = (id: string) => `${base}/${encodeURIComponent(id)}`;
export const evolutionProgramsApi = {
  getProgramMemories: (id: string, signal?: AbortSignal) => request<{memory: SemanticMemoryHistory}>(`${base}/${encodeURIComponent(id)}/memory`, {signal}),
  listPrograms: (signal?: AbortSignal, query: ProgramCatalogQuery = {}) => request<ProgramCatalogPage>(base, {signal, query: {...query}}),
  createProgram: (body: CreateProgramBody, key: string) => request<{program: ExplorationProgram}>(base,
    {method: 'POST', json: body, headers: {'Idempotency-Key': key}}),
  getProgram: (id: string, signal?: AbortSignal) => request<{program: ExplorationProgram}>(programPath(id), {signal}),
  commandProgram: (id: string, action: ProgramAction) => request<{program: ExplorationProgram}>(
    `${programPath(id)}/commands`, {method: 'POST', json: {action}}),
  setProgramPacing: (id: string, advance_mode: ProgramAdvanceMode) => request<{program: ExplorationProgram}>(
    `${programPath(id)}/commands`, {method: 'POST', json: {action: 'set_pacing', advance_mode}}),
  appendProgramSteps: (id: string, steps: ExplorationStep[], key: string) => request<{program: ExplorationProgram}>(
    `${programPath(id)}/steps`, {method: 'POST', json: {steps}, headers: {'Idempotency-Key': key}}),
  getProgramExperiments: (id: string, after = 0, limit = 20, signal?: AbortSignal) => request<ExperimentPage>(
    `${programPath(id)}/experiments`, {query: {after, limit}, signal}),
};
