/** Evolution read model: one visible-page refresh flight, bounded server projections.
 * Dependencies are injected API, clock and visibility ports. No DOM or durable state.
 */
import { runWithConcurrency } from '../../core/async-pool';
import type { evolutionApi, EvolutionGoal, EvolutionPlan, EvolutionEpisode, EvolutionMutation, EvolutionCapabilityGap, EvolutionSkill } from '../../api/evolution';
export type EvolutionApi = typeof evolutionApi;
export interface EvolutionState {
  goals: EvolutionGoal[]; plans: EvolutionPlan[]; episodes: EvolutionEpisode[];
  mutations: EvolutionMutation[]; gaps: EvolutionCapabilityGap[]; skills: EvolutionSkill[];
  snapshot?: Awaited<ReturnType<EvolutionApi['snapshot']>>['snapshot'];
}
export const emptyState = (): EvolutionState => ({ goals: [], plans: [], episodes: [], mutations: [], gaps: [], skills: [] });
export function createEvolutionController(api: EvolutionApi, ports: {
  update: (state: EvolutionState, errors: string[]) => void;
  visible: () => boolean;
  schedule: (callback: () => void, delay: number) => number;
  cancel: (id: number) => void;
}) {
  let state = emptyState();
  let disposed = false;
  let timer = 0;
  let flight: Promise<void> | undefined;
  const abort = new AbortController();
  const stopTimer = () => { if (timer) ports.cancel(timer); timer = 0; };
  const refresh = (): Promise<void> => {
    if (disposed || !ports.visible()) return Promise.resolve();
    if (flight) return flight;
    stopTimer();
    flight = (async () => {
      const signal = abort.signal;
      const reads = [
        ['goals', () => api.listGoals(signal).then(r => r.goals)],
        ['plans', () => api.listPlans(undefined, signal).then(r => r.plans)],
        ['episodes', () => api.listEpisodes(signal).then(r => r.episodes)],
        ['mutations', () => api.listMutations(signal).then(r => r.mutations)],
        ['gaps', () => api.listCapabilityGaps(signal).then(r => r.capability_gaps)],
        ['skills', () => api.listSkills(signal).then(r => r.skills)],
        ['snapshot', () => api.snapshot(signal).then(r => r.snapshot)],
      ] as const;
      const errors: string[] = [];
      const next = { ...state };
      await runWithConcurrency<(typeof reads)[number]>(reads, async ([key, read]) => {
        if(disposed || !ports.visible())return;
        try {
          const value = await read();
          Object.assign(next, { [key]: Array.isArray(value) ? value.slice(0, 200) : value });
        } catch(reason) { errors.push(`${key}: ${reason instanceof Error ? reason.message : String(reason)}`); }
      }, 4);
      if (!disposed) { state = next; ports.update(state, errors); }
    })().finally(() => {
      flight = undefined;
      if (!disposed && ports.visible()) timer = ports.schedule(() => { void refresh(); }, 15000);
    });
    return flight;
  };
  return {
    refresh,
    // A write must not reuse a read that started before its authoritative receipt.
    async refreshAfterWrite() { if (flight) await flight; await refresh(); },
    visibilityChanged: () => { stopTimer(); if (ports.visible()) void refresh(); },
    dispose: () => { disposed = true; stopTimer(); abort.abort(); },
  };
}
