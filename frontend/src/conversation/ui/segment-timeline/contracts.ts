/** Timeline display inputs. Canonical Sync values stay immutable; callbacks own UI and shared grouping policy. */
import type { ToolGroupRound, ToolGroupSession, ToolNarrationSegment } from '../tool-groups/contracts';
import type { ToolExecutionBatch } from '../../presentation/tool-execution-groups';
export interface TimelineRound extends ToolGroupRound {
  readonly todoDisplayUpdateCount?: number; readonly toolContent?: unknown; readonly _swarmActive?: boolean;
  readonly steerRound?: number; readonly peerRound?: number; readonly inboxRound?: number;
  readonly bgCommandRound?: number; readonly stallRound?: number;
}
export interface TimelineSegment extends ToolNarrationSegment {
  readonly id?: string | number; readonly terminal?: boolean;
  readonly noteKind?: string; readonly reason?: string; readonly action?: string;
}
export interface TimelineBatch {
  key: string; llmRound: number | string | null; segs: TimelineSegment[]; rounds: TimelineRound[];
  attemptOrdinal: number; totalAttempts: number; contextNoteGroup?: boolean;
}
export interface TimelinePorts {
  groupSegments(segments: readonly TimelineSegment[], hasLlm: boolean): readonly ToolExecutionBatch<TimelineSegment>[];
  isSuperseded(round: TimelineRound): boolean;
  createSession(rounds: readonly TimelineRound[]): ToolGroupSession;
  renderBatch(segments: readonly TimelineSegment[], rounds: readonly TimelineRound[],
    allRounds: readonly TimelineRound[], renderGroups: (rounds: readonly ToolGroupRound[]) => string): string;
  panel(rounds: readonly TimelineRound[], allRounds: readonly TimelineRound[], active: boolean): {
    active: boolean; collapsed: boolean; attention: string; html: string;
  };
}
