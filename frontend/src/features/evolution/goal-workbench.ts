/** Factual goal observation projection. No timers, percentages or scene state.
 * Run/receipt status is authoritative; a task's stage is never its current status.
 * Used by the persistent mission workspace and the detailed run inspector.
 */
import type { EvolutionRun, GoalWorkspace, GoalCoordination } from '../../api/evolution-runs';

export type StudioCopy = (key: string) => string;
export interface GoalObservation {
  run?: EvolutionRun;
  workspace?: GoalWorkspace;
  coordination?: GoalCoordination;
  questions?: number;
  loading?: boolean;
}
export const isTerminalRun = (run: Pick<EvolutionRun, 'status'>): boolean =>
  ['completed', 'failed', 'needs_attention', 'cancelled'].includes(run.status);

export function runStatusLabel(status: string, copy: StudioCopy): string {
  const key: Record<string, string> = {
    done: 'completed', success: 'completed', error: 'failed', stopped: 'cancelled',
    preparing: 'missionPreparing', settling: 'missionSettling',
    executing: 'executing', reviewing: 'reviewing', completed: 'completed',
    failed: 'failed', needs_attention: 'needs_attention', cancelled: 'cancelled',
    running: 'running', pending: 'pending', queued: 'queued', paused: 'paused',
  };
  return copy(key[status] || 'missionStatusUnknown');
}

export function failureReason(error: string | undefined, copy: StudioCopy): string {
  if (!error) return '';
  const code = error.split(/[;:]/, 1)[0].trim().toLowerCase();
  if (code === 'execution_token_usage_unknown') return copy('missionUsageUnknown');
  if (/budget|token_limit|max_tokens|tool_call_limit|wall_clock|timeout|time_limit/.test(code)) return copy('missionFailureLimit');
  const keys: Record<string, string> = {
    task_unavailable: 'missionFailureUnavailable',
    stage_handoff_interrupted: 'missionFailureInterrupted',
    stage_admission_deferred: 'missionStageAdmissionPending',
    stage_launch_recovery_refused: 'missionStageRecoveryRefused',
    stage_loss_recovery_refused: 'missionStageLossRecoveryRefused',
    review_worker_lost: 'missionStageAdmissionPending',
    stage_outcome_recovery_refused: 'sourceRecoveryRefused',
    pending_stage_restoration_refused: 'pendingRestorationRefused',
    task_terminal_delivery_unverified: 'sourceTerminalUnverified',
    intake_failed: 'missionFailureIntake',
    delivery_empty_or_exceeds_inline_limit: 'missionFailureDelivery',
    unacknowledged_goal_guidance: 'missionFailureGuidance',
    research_record_unavailable_or_not_terminal: 'missionFailureResearch',
    cancelled: 'missionFailureStopped', stopped: 'missionFailureStopped',
  };
  // Unknown provider/legacy errors remain available in technical details, never
  // presented as an untranslated enum in the primary status or mission header.
  return copy(keys[code] || 'missionFailureUnknown');
}

export function measuredCount(value: number | null | undefined): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0;
}
export function limitLabel(value: number | null | undefined, copy: StudioCopy): string {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value.toLocaleString() : copy('missionNoLimit');
}
export function runTelemetry(run: EvolutionRun, workspace: GoalWorkspace | undefined, copy: StudioCopy) {
  const budget = workspace?.budget ?? run.budget;
  const lower = run.actuals_complete === false ? '≥ ' : '';
  return {
    tokens: lower + measuredCount(run.actuals?.tokens).toLocaleString(),
    calls: lower + measuredCount(run.actuals?.tool_calls).toLocaleString(),
    seconds: lower + measuredCount(run.actuals?.wall_clock_seconds).toLocaleString(),
    tokenLimit: limitLabel(budget?.max_tokens, copy),
    callLimit: limitLabel(budget?.max_tool_calls, copy),
    timeLimit: limitLabel(budget?.max_wall_clock_seconds, copy),
  };
}

export function taskRecords(run: EvolutionRun, copy: StudioCopy) {
  const records = new Map<string, {taskId: string; stage: string; status: string; outcomeAvailable: boolean; restoredFromOutcome: boolean; reviewFormatNote: string}>();
  for (const receipt of run.receipts || []) {
    if (!receipt.task_id) continue;
    records.set(receipt.task_id, {
      taskId: receipt.task_id,
      reviewFormatNote: receipt.review_format_retry ? copy(receipt.review_format_retry.error === 'originals_uninspected'
        ? receipt.review_format_retry.admitted ? 'reviewOriginalsRetryAdmitted' : 'reviewOriginalsRetryRefused'
        : receipt.review_format_retry.admitted ? 'reviewFormatRetryAdmitted' : 'reviewFormatRetryRefused') : '',
      stage: copy(receipt.stage === 'reviewing' ? 'reviewer' : receipt.stage === 'executing' ? 'executor' : 'executionRecords'),
      outcomeAvailable: receipt.submission_voided !== true && receipt.outcome_available === true, restoredFromOutcome: receipt.submission_voided !== true && receipt.restored_from_outcome === true,
      status: receipt.submission_voided === true ? copy('sourceNotSubmitted') : receipt.terminal_verdict ? copy({completed:'sourceCompletedShort',interrupted:'sourceInterrupted',
        truncated:'sourceTruncated',failed:'sourceFailed'}[receipt.terminal_verdict]) : runStatusLabel(receipt.task_id === run.task_id && isTerminalRun(run) && !['completed','failed','cancelled','success','error'].includes(receipt.status || '') ? run.status : receipt.status || 'unknown', copy),
    });
  }
  if (run.task_id && !records.has(run.task_id)) {
    records.set(run.task_id, {taskId: run.task_id, stage: copy('inspectTask'), status: runStatusLabel(run.status, copy), outcomeAvailable: false, restoredFromOutcome: false, reviewFormatNote: ''});
  }
  return [...records.values()].reverse();
}

export function latestGoalUpdate(coordination?: GoalCoordination) {
  return [...(coordination?.updates || [])].filter(item => item.origin !== 'user' && item.content.trim())
    .sort((a, b) => b.seq - a.seq)[0];
}

export function reviewOutcomeLabel(outcome: string, copy: StudioCopy): string {
  const key: Record<string, string> = {success: 'missionReviewPassed', pass: 'missionReviewPassed',
    failure: 'missionReviewFailed', failed: 'missionReviewFailed', fail: 'missionReviewFailed',
    partial: 'missionReviewPartial', inconclusive: 'missionReviewUnverified', unmeasurable: 'missionReviewUnverified'};
  return copy(key[outcome] || 'missionReviewUnverified');
}
