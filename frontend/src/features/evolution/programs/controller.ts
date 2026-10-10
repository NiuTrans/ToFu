/** Visible-page exploration reads. One flight, one trailing refresh, no writes.
 * Route epochs fence stale responses. Program, execution and receipt pages stay
 * server-owned; this controller never manufactures progress or starts work. */
import type { EvolutionApi } from '../controller';
import type { ExplorationProgram, ExplorationProgramSummary, ExperimentPage, ExplorationReceipt, SemanticMemoryHistory } from '../../../api/evolution-programs';
import type { EvolutionRun, HumanQuestion, EvolutionModel } from '../../../api/evolution-runs';

export interface ProgramState {
  programs: ExplorationProgramSummary[];
  catalogNext?: string | null;
  catalogPrevious?: string | null;
  program?: ExplorationProgram;
  run?: EvolutionRun;
  runError?: string;
  questions: HumanQuestion[];
  history?: ExperimentPage;
  memories?: SemanticMemoryHistory;
  memoryError?: string;
  finalReceipt?: ExplorationReceipt;
  deliveryError?: string;
  historyAfter: number;
  models: Array<{model: EvolutionModel; label: string}>;
  loading: boolean;
  catalogPending: boolean;
  historyPending: boolean;
  errors: string[];
}
export type ProgramApi = Pick<EvolutionApi, 'listPrograms' | 'getProgram' | 'getProgramExperiments'
  | 'getProgramMemories' | 'getRun' | 'listRunModels' | 'createProgram' | 'commandProgram' | 'appendProgramSteps' | 'answerRun'>
  & Partial<Pick<EvolutionApi, 'getStageOutcome' | 'setProgramPacing'>>;
export const emptyProgramState = (): ProgramState => ({programs: [], questions: [], models: [],
  historyAfter: 0, loading: false, catalogPending: false, historyPending: false, errors: []});

export function createProgramController(api: ProgramApi, ports: {
  update: (state: ProgramState) => void;
  visible: () => boolean;
  schedule: (callback: () => void, delay: number) => number;
  cancel: (id: number) => void;
}) {
  let state = emptyProgramState();
  let selected = '';
  let historyAfter: number | undefined;
  let catalogQuery: {after?: string; before?: string; limit: number} = {limit: 20};
  let epoch = 0;
  let disposed = false;
  let timer = 0;
  let flight: Promise<void> | undefined;
  let trailing = false;
  let abort: AbortController | undefined;
  const clearTimer = () => { if (timer) ports.cancel(timer); timer = 0; };
  const emit = () => { if (!disposed) ports.update(state); };
  const message = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);

  function refresh(): Promise<void> {
    if (disposed || !ports.visible()) return Promise.resolve();
    if (flight) return flight;
    clearTimer();
    const captured = epoch, id = selected, requestedAfter = historyAfter;
    const requestAbort = new AbortController();
    abort = requestAbort;
    state = {...state, loading: true, errors: []}; emit();
    flight = (async () => {
      const errors: string[] = [];
      const next: ProgramState = {...state, loading: false, catalogPending: false, historyPending: false};
      const signal = requestAbort.signal;
      // Selected detail and catalogs are independent reads in the same flight.
      // Wrap detail invocation so synchronous adapter errors remain contained.
      const catalog = await Promise.allSettled([api.listPrograms(signal, catalogQuery), api.listRunModels(signal),
        id ? Promise.resolve().then(() => api.getProgram(id, signal)) : Promise.resolve(null)]);
      if (disposed || captured !== epoch || signal.aborted) return;
      if (catalog[0].status === 'fulfilled') {
        next.programs = catalog[0].value.programs.slice(0, 50);
        next.catalogNext = catalog[0].value.next_cursor ?? null;
        next.catalogPrevious = catalog[0].value.previous_cursor ?? null;
      }
      else errors.push(message(catalog[0].reason));
      if (catalog[1].status === 'fulfilled') next.models = catalog[1].value.models.slice(0, 200);
      else errors.push(message(catalog[1].reason));
      if (id) {
        try {
          const detailRead = catalog[2];
          if (detailRead.status === 'rejected') throw detailRead.reason;
          const detail = detailRead.value;
          if (!detail) throw new Error('program_detail_unavailable');
          if (disposed || captured !== epoch || signal.aborted) return;
          if (detail.program.program_id !== id) throw new Error('program_identity_mismatch');
          next.program = detail.program;
          const after = requestedAfter ?? Math.max(0, detail.program.settled_through - 20);
          const completed = detail.program.status === 'completed';
          const terminalSequence = detail.program.settled_through;
          const finalInHistory = terminalSequence > 0 && after < terminalSequence && terminalSequence <= after + 20;
          const matchesDelivery = (receipt?: ExplorationReceipt) => !!receipt && completed
            && receipt.program_id === detail.program.program_id && receipt.sequence === terminalSequence
            && receipt.step?.kind === 'final' && receipt.outcome === 'completed';
          // Retention is scoped to this confirmed terminal identity. A failed
          // read cannot delete evidence; a newer terminal identity cannot reuse it.
          if (!matchesDelivery(next.finalReceipt)) next.finalReceipt = undefined;
          next.deliveryError = undefined;
          const reads = await Promise.allSettled([
            api.getProgramExperiments(id, after, 20, signal),
            detail.program.current ? api.getRun(detail.program.current.run_id, signal) : Promise.resolve(null),
            completed && terminalSequence > 0 && !finalInHistory
              ? api.getProgramExperiments(id, detail.program.settled_through - 1, 1, signal) : Promise.resolve(null),
            api.getProgramMemories(id, signal),
          ]);
          if (disposed || captured !== epoch || signal.aborted) return;
          if (reads[0].status === 'fulfilled' && Array.isArray(reads[0].value?.experiments)
              && reads[0].value.experiments.every(receipt => receipt?.program_id === id)) {
            next.history = reads[0].value; next.historyAfter = after;
          } else errors.push(reads[0].status === 'rejected' ? message(reads[0].reason) : 'history_identity_mismatch');
          const expectedRun = detail.program.current?.run_id;
          if (reads[1].status === 'fulfilled' && (!expectedRun || reads[1].value?.run?.run_id === expectedRun)) {
            next.run = reads[1].value?.run; next.questions = reads[1].value?.pending_questions || [];
            next.runError = undefined;
          } else {
            // A transport failure says nothing about already confirmed human
            // questions. Keep their drafts only while the run owner matches.
            next.runError = reads[1].status === 'rejected' ? message(reads[1].reason) : 'run_identity_mismatch';
            if (next.run?.run_id !== expectedRun) {
              next.run = undefined; next.questions = [];
            }
            errors.push(next.runError);
          }
          if (completed) {
            const deliveryRead = finalInHistory ? reads[0] : reads[2];
            if (deliveryRead.status === 'rejected') {
              next.deliveryError = message(deliveryRead.reason);
              if (!finalInHistory) errors.push(next.deliveryError);
            } else {
              // Successful absence or a changed owner/sequence/verdict is not
              // a transport outage. Never label unrelated work as the delivery.
              const page = deliveryRead.value;
              next.finalReceipt = page?.settled_through === terminalSequence
                ? page.experiments.find(receipt => matchesDelivery(receipt)) : undefined;
              if (!next.finalReceipt) next.deliveryError = 'delivery_receipt_missing_or_changed';
            }
          }
          if (reads[3].status === 'fulfilled') { next.memories = reads[3].value.memory; next.memoryError = undefined; }
          else { next.memoryError = message(reads[3].reason); }
        } catch (reason) { errors.push(message(reason)); }
      } else {
        next.program = undefined; next.history = undefined; next.finalReceipt = undefined; next.deliveryError = undefined; next.run = undefined; next.runError = undefined; next.questions = []; next.memories = undefined; next.memoryError = undefined;
      }
      if (!disposed && captured === epoch && !signal.aborted) { state = {...next, errors}; emit(); }
    })().finally(() => {
      flight = undefined; if (abort === requestAbort) abort = undefined;
      if (disposed) return;
      if (trailing) { trailing = false; void refresh(); }
      else if (ports.visible()) {
        const active = state.program?.status === 'active' || ((state.program?.status === 'paused' || state.program?.status === 'cancelled') && !!state.program.current)
          || state.programs.some(program => program.status === 'active');
        timer = ports.schedule(() => { timer = 0; void refresh(); }, active ? 5000 : 30000);
      }
    });
    return flight;
  }

  function invalidate() {
    epoch += 1; clearTimer(); abort?.abort();
    state = {...state, program: undefined, run: undefined, runError: undefined, questions: [], history: undefined, finalReceipt: undefined, deliveryError: undefined, memories: undefined, memoryError: undefined,
             historyAfter: historyAfter || 0, loading: true, catalogPending: false, historyPending: false, errors: []};
    emit();
    if (flight) trailing = true;
    else void refresh();
  }
  return {
    refresh,
    select(id: string) {
      if (selected === id) { void refresh(); return; }
      selected = id; historyAfter = undefined; invalidate();
    },
    page(after?: number) {
      if (state.historyPending) return;
      historyAfter = after;
      epoch += 1; clearTimer(); abort?.abort();
      // Changing a read window does not change the selected program. Keep
      // its known evidence and stable controls until the new page is admitted.
      state = {...state, historyPending: true, loading: true, errors: []}; emit();
      if (flight) trailing = true;
      else void refresh();
    },
    catalogPage(direction: 'first' | 'next' | 'previous') {
      if (state.catalogPending) return;
      const cursor = direction === 'next' ? state.catalogNext : state.catalogPrevious;
      if (direction !== 'first' && !cursor) return;
      if (direction === 'first' && !state.catalogNext && !state.catalogPrevious) return;
      catalogQuery = {limit: 20, ...(direction === 'next' ? {after: cursor!}
        : direction === 'previous' ? {before: cursor!} : {})};
      epoch += 1; clearTimer(); abort?.abort();
      // Catalog navigation preserves the selected program, full originals,
      // current receipt page and every stable write form.
      state = {...state, catalogPending: true, loading: true, errors: []}; emit();
      if (flight) trailing = true;
      else void refresh();
    },
    async afterWrite() {
      // A pre-write read may resolve successfully but cannot overwrite a write.
      epoch += 1; clearTimer(); abort?.abort();
      if (flight) { trailing = true; await flight; if (flight) await flight; }
      else if (!disposed) await refresh();
    },
    visibilityChanged() {
      clearTimer();
      if (!ports.visible()) abort?.abort();
      else { if (flight) trailing = true; else void refresh(); }
    },
    dispose() { disposed = true; epoch += 1; clearTimer(); abort?.abort(); },
  };
}
