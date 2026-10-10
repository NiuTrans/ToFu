/** Typed client for the self-evolution API surface (routes/api_v1/evolution.py). */
import { request } from './transport';
import { evolutionRunsApi } from './evolution-runs';
import { evolutionProgramsApi } from './evolution-programs';

export interface EvolutionGoal {
  goal_id: string;
  statement: string;
  domain: string;
  exploratory: boolean;
  status: string;
  created_at: number;
  criteria?: Array<{ criterion_id: string; kind: string; params: Record<string, unknown> }>;
  budget_envelope?: { max_tokens: number; max_wall_clock_seconds: number; max_tool_calls: number };
}

export interface EvolutionPlan {
  plan_id: string;
  goal_id: string;
  status: string;
  selected_path_id: string;
  selection_rationale?: string;
  candidates: Array<{
    path_id: string;
    title: string;
    utility_over_cost: number;
    cost_estimate: { priors_source: string; expected_tokens: number;
      expected_wall_clock_seconds?: number; expected_tool_calls?: number };
    stages?: Array<{ kind: string; detail: string }>;
  }>;
}

export interface EvolutionEpisode {
  episode_id: string;
  goal_id: string;
  task_id: string;
  status: string;
  actuals: { tokens: number; wall_clock_seconds: number; tool_calls: number };
  actuals_complete?: boolean;
  settled_at?: number | null;
  plan_id?: string;
  exploratory?: boolean;
  stage_receipts?: Array<{ stage: string; status: string; detail?: string }>;
  verdict?: { outcome: string; assessed_by: string;
    criterion_results: Array<{ criterion_id: string; outcome: string; evidence_ref?: string }>;
    evidence_refs?: string[] } | null;
  learning?: { resource_weight_deltas?: Array<{ ref_id: string; delta: number; basis: string }>;
    cost_actual_vs_estimate?: { tokens_ratio: number; wall_clock_ratio: number } | null;
    strategy_outcome?: { template_id: string; outcome: string } | null;
    capability_gaps?: Array<{ summary: string; proposed_action: string }> };
}

export interface EvolutionActivationIntent {started_at: number; context_fingerprint: string}

export interface EvolutionMutation {
  mutation_id: string;
  kind: string;
  status: string;
  version: number;
  parent_version?: number;
  payload?: Record<string, unknown>;
  evidence_episode_ids?: string[];
  rejection_reason?: string;
  activation_intent?: EvolutionActivationIntent;
}

export interface EvolutionCapabilityGap {
  gap_id: string;
  summary: string;
  proposed_action: string;
  samples: number;
  status: string;
}

export interface EvolutionSkill {
  skill_id: string;
  name: string;
  description: string;
  status: string;
  sandbox_receipt?: { verdict: string; output: string } | null;
}

export const evolutionApi = {
  ...evolutionRunsApi,
  ...evolutionProgramsApi,
  listGoals: (signal?: AbortSignal) =>
    request<{ goals: EvolutionGoal[] }>('/api/v4/evolution/goals', { signal }),
  createGoal: (goal: Record<string, unknown>) =>
    request<{ goal: EvolutionGoal }>('/api/v4/evolution/goals', {
      method: 'POST', json: goal }),
  listPlans: (goalId?: string, signal?: AbortSignal) =>
    request<{ plans: EvolutionPlan[] }>('/api/v4/evolution/plans', {
      signal, query: goalId ? { goal_id: goalId } : {} }),
  proposePlan: (goalId: string) =>
    request<{ plan: EvolutionPlan }>(
      `/api/v4/evolution/goals/${encodeURIComponent(goalId)}/plans`,
      { method: 'POST', json: {} }),
  confirmPlan: (planId: string) =>
    request<{ plan: EvolutionPlan }>(
      `/api/v4/evolution/plans/${encodeURIComponent(planId)}/confirm`,
      { method: 'POST', json: {} }),
  rejectPlan: (planId: string) =>
    request<{ plan: EvolutionPlan }>(
      `/api/v4/evolution/plans/${encodeURIComponent(planId)}/reject`,
      { method: 'POST', json: {} }),
  goalReport: (goalId: string) =>
    request<{ report: string }>(
      `/api/v4/evolution/goals/${encodeURIComponent(goalId)}/report`),
  launchPlan: (planId: string) =>
    request<{ episode: EvolutionEpisode; task_id: string }>(
      `/api/v4/evolution/plans/${encodeURIComponent(planId)}/launch`,
      { method: 'POST', json: {} }),
  listEpisodes: (signal?: AbortSignal) =>
    request<{ episodes: EvolutionEpisode[] }>('/api/v4/evolution/episodes', { signal }),
  taskStatus: (taskId: string) => request<{status: string}>(`/api/v4/tasks/${encodeURIComponent(taskId)}`),
  /** Metadata-only request fold for one task (rounds, attempts, tool names).
   * Backs the live single/floor workbench views; never carries payloads. */
  taskRequests: (taskId: string) =>
    request<{ eventsAvailable?: boolean; requestCount?: number; requests?: Array<{
      roundNum?: number; turn?: string;
      toolNames?: string[];
      attempts?: Array<{ tokensIn?: number; tokensOut?: number }>;
    }> }>(`/api/v4/tasks/${encodeURIComponent(taskId)}/requests`, { onError: 'null' }),
  settleVerdict: (episodeId: string, outcome: string, criterionResults: Array<{criterion_id: string; outcome: string; evidence_ref: string}> = []) =>
    request<{ episode: EvolutionEpisode }>(
      `/api/v4/evolution/episodes/${encodeURIComponent(episodeId)}/verdict`,
      { method: 'POST', json: { outcome, criterion_results: criterionResults } }),
  listMutations: (signal?: AbortSignal) =>
    request<{ mutations: EvolutionMutation[] }>('/api/v4/evolution/mutations', { signal }),
  mutationAction: (mutationId: string, action: string, reason?: string) =>
    request<{ mutation: EvolutionMutation;
      final_status?: string; receipt?: unknown }>(
      `/api/v4/evolution/mutations/${encodeURIComponent(mutationId)}/${encodeURIComponent(action)}`,
      { method: 'POST', json: reason ? { reason } : {} }),
  listCapabilityGaps: (signal?: AbortSignal) =>
    request<{ capability_gaps: EvolutionCapabilityGap[] }>(
      '/api/v4/evolution/capability-gaps', { signal }),
  recompute: () =>
    request<Record<string, unknown>>('/api/v4/evolution/recompute', {
      method: 'POST', json: {} }),
  forgeSkill: (input: { name: string; description: string;
    knowledge?: string; examples?: string[]; gap_id?: string }) =>
    request<{ skill: EvolutionSkill }>('/api/v4/evolution/skills/forge', {
      method: 'POST', json: input }),
  skillAction: (skillId: string, action: string,
    body?: Record<string, unknown>) =>
    request<{ skill?: EvolutionSkill; mutation?: EvolutionMutation }>(
      `/api/v4/evolution/skills/${encodeURIComponent(skillId)}/${encodeURIComponent(action)}`,
      { method: 'POST', json: body || {} }),
  listSkills: (signal?: AbortSignal) =>
    request<{ skills: EvolutionSkill[] }>('/api/v4/evolution/skills', { signal }),
  teach: (input: { name: string; description: string;
    knowledge?: string; examples?: string[]; gap_id?: string }) =>
    request<{ skill: EvolutionSkill; episode: EvolutionEpisode;
      verdict: { outcome: string }; mutation: EvolutionMutation | null }>(
      '/api/v4/evolution/teach', { method: 'POST', json: input }),
  snapshot: (signal?: AbortSignal) =>
    request<{ snapshot: {
      episodes: { settled: number; running: number };
      domains: Record<string, { samples: number; failure_rate: number }>;
      admitted_factors: Array<{ statement: string; lift: number }>;
      pending_hypotheses: number;
      capability_gaps: Array<{ summary: string; samples: number }>;
      strategies: number;
      anti_patterns: number;
      active_mutations: Array<{ kind: string; version: number }>;
      installed_skills: string[];
    } }>('/api/v4/evolution/snapshot', { signal }),
} as const;
