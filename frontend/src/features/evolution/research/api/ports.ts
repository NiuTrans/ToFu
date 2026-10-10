/** Typed ports over the existing authenticated task/research API; no transport. */
import type { JsonObject } from "../state/stream";
export interface TaskApi {
  start(kind: string, payload: JsonObject): Promise<JsonObject>;
  events?(taskId: string, cursor: number): Promise<JsonObject>;
  get(taskId: string): Promise<JsonObject>;
  abort(taskId: string): Promise<unknown>;
  list?(kind: string, status?: string): Promise<JsonObject>;
}

export interface ResearchApi {
  record?(taskId: string): Promise<JsonObject>;
  records?(limit: number): Promise<JsonObject>;
  resumeRecord?(taskId: string): Promise<JsonObject>;
  lookup(direction: string, lang: string): Promise<JsonObject>;
}

/** Evolution run creation seam: the research workbench launches its pipeline
 * as a research-domain evolution run, never as a bare task. Registered by the
 * page composition root with the typed client; tests substitute their own. */
export interface EvolutionRunsPort {
  createRun(body: {
    objective: string;
    domain: 'research';
    research: {
      lang: string;
      harvest_n?: number;
      model?: string;
    };
  }, idempotencyKey: string): Promise<{
    run: { run_id: string; task_id: string; status: string; error?: string };
  }>;
  cancelRun(runId: string): Promise<{run: {run_id: string}; ok?: boolean; error?: unknown}>;
}
