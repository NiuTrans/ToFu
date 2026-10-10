/** Presentation of confirmed limits; server commands remain authoritative. */
import type { ExplorationProgram } from '../../../api/evolution-programs';
export function programLimitHint(program: ExplorationProgram): string | null {
  const budget = program.execution_options?.budget;
  if (!budget) return null;

  const startedAt = program.started_at === undefined ? program.created_at : program.started_at;
  if (budget.max_tokens && program.actuals_complete === false) return 'limitUnknown';
  if (budget.max_tool_calls && (program.tool_calls_complete ?? program.actuals_complete) === false) return 'policyRemainingUnknown';
  if (budget.max_tokens && program.actuals.tokens >= budget.max_tokens) return 'limitTokens';
  if (budget.max_tool_calls && program.actuals.tool_calls >= budget.max_tool_calls) return 'limitTools';
  // Use the observed server timestamp, never a browser clock or another timer.
  if (budget.max_wall_clock_seconds && startedAt !== null && (program.updated_at >= startedAt + budget.max_wall_clock_seconds
    || program.error === 'program limit reached: max_wall_clock_seconds'
    || program.error === 'execution_deadline_exceeded')) return 'limitTime';
  const reserve = program.execution_options?.review_reserve;
  if ((budget.max_tokens && reserve?.tokens && budget.max_tokens - program.actuals.tokens <= reserve.tokens)
    || (budget.max_tool_calls && reserve?.tool_calls && budget.max_tool_calls - program.actuals.tool_calls <= reserve.tool_calls)
    || (budget.max_wall_clock_seconds && reserve?.wall_clock_seconds
      && budget.max_wall_clock_seconds - (startedAt === null ? 0 : Math.max(0, program.updated_at - startedAt)) <= reserve.wall_clock_seconds)) return 'limitReviewReserve';
  return null;
}
