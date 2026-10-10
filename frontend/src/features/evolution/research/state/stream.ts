/** Ephemeral task projection only; durable research lives on the server. */
import type { ResearchProgressV1 } from '../../../../api/research-progress.generated';
import type { PaperPushState } from "../../../paper/push-transport";
import type { ResearchCycle } from "../../../paper/research-cycle";
export type JsonObject = Record<string, unknown>;
export type ResearchStatus = 'submitting' | 'pending' | 'running' | 'done' | 'error' | 'aborted' | 'degraded' | 'interrupted';

interface ToolRound extends JsonObject {
  roundNum?: unknown;
  toolName?: unknown;
  query?: unknown;
  toolCallId?: unknown;
  status?: string;
  _elapsed?: string;
}

export interface ResearchStream extends PaperPushState, JsonObject {
  direction: string;
  taskId: string | null;
  evolutionRunId: string | null;
  status: ResearchStatus;
  phase: string;
  startedAt: number;
  lastEventAt: number;
  degraded: boolean;
  degradedReason: string;
  gateReached: string;
  accepted: number;
  rejected: number;
  corpusSize: number;
  corpusIds: string[];
  papers?: JsonObject[];

  citationEdges?: JsonObject[];
  sourceStatuses?: JsonObject[];
  literatureSort?: import('./literature').LiteratureSort;
  literatureView?: 'list' | 'graph';
  failedStage?: string;

  surveyBatchFailures?: JsonObject[];

  verdictAvailable?: boolean;
  recordSaved?: boolean;
  folderId: string;
  error: string;
  acceptedIdeas: JsonObject[];
  rejectedIdeas: JsonObject[];
  threshold: unknown;
  surveyMd: string;
  openGaps: JsonObject | null;
  evaluation: JsonObject | null;

  researchCycle?: ResearchCycle | null;
  usage: JsonObject | null;
  toolRounds: ToolRound[];
  hydrated: boolean;
  reconnecting?: boolean;
  progress?: ResearchProgressV1;
  cancelling?: boolean;
  commandError?: string;
  lang?: string;
  idempotencyKey?: string;
}

export function newResearchStream(direction: string): ResearchStream {
  return {
    direction,
    taskId: null,
    evolutionRunId: null,
    status: 'pending',
    phase: '',
    startedAt: 0,
    lastEventAt: 0,
    degraded: false,
    degradedReason: '',
    gateReached: '',
    accepted: 0,
    rejected: 0,
    corpusSize: 0,
    corpusIds: [],
    folderId: '',
    error: '',
    acceptedIdeas: [],
    rejectedIdeas: [],
    threshold: null,
    surveyMd: '',
    openGaps: null,
    evaluation: null,
    usage: null,
    toolRounds: [],
    hydrated: false,
    _seqSeen: -1,
    _replayCursor: 0,
  };
}
