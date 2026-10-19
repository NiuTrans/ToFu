/** Historical native Goal settlement compatibility, not prose deduplication.
 * Older stores sealed an unmasked VU terminal owner then appended its sanitized
 * completion. Select that canonical completion without mutating durable segments
 * or toolRounds. Current backend settlement owns prospective normalization. */
import type { TurnContentSegment, TurnRecord } from '../../../api/conversation-sync.generated';

import { stripAgentMachineTokens } from '../agent-machine-tokens.generated';

export function selectTerminalPresentationSegments(
  turn: Pick<TurnRecord, 'actor'>,
  segments: ReadonlyArray<TurnContentSegment>,
): ReadonlyArray<TurnContentSegment> {
  if (turn.actor !== 'virtual_user') return segments;
  const completion = segments.at(-1);
  if (!completion || completion.type !== 'text' || !completion.terminal
      || !/^text:completion-\d+$/.test(completion.blockId)) return segments;
  let priorIndex = segments.length - 2;
  while (priorIndex >= 0 && segments[priorIndex].type !== 'text') priorIndex -= 1;
  const prior = segments[priorIndex];
  if (!prior || prior.type !== 'text' || prior.blockId !== 'text:terminal'
      || prior.terminal || prior.deliverable) return segments;
  const sanitized = stripAgentMachineTokens(prior.text);
  // A registered machine token must explain the entire difference. Repeated
  // prose, partial overlap or a genuinely changed answer has no deletion claim.
  return sanitized !== prior.text && sanitized === completion.text
    ? segments.filter((_, index) => index !== priorIndex) : segments;
}
