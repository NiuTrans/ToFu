/** Live per-episode activity projection for the Evolution workbench.
 *
 * A running episode is backed by one durable task. The task's request fold
 * (`Api.tasks.getRequests`) already records each model round, its attempts and
 * the tools it called. This module folds that record, plus the episode's
 * stage receipts, into a chat-like live view model — a goal message, a visible
 * streaming stage, and tool rounds — without any debug-panel chrome.
 *
 * It is presentation-only and never mutates state; the workbench re-reads it
 * on each bounded refresh while an episode is live.
 */
import { esc, copy } from './ui';
import { t } from '../../../i18n';

export interface ActivityRound {
  roundNum: number;
  turn: string;
  toolNames: string[];
  tokens: number;
  running: boolean;
}

export interface EpisodeActivity {
  /** Latest stage label for the streaming indicator. */
  stage: string;
  stageDone: boolean;
  /** Tool/model rounds folded from the task record, oldest first. */
  rounds: ActivityRound[];
  /** True once the task reported at least one persisted round. */
  hasRounds: boolean;
}

interface TaskRequestRow {
  roundNum?: number;
  turn?: string;
  toolNames?: string[];
  attempts?: Array<{ tokensIn?: number; tokensOut?: number }>;
}

/** Fold raw task request rows into the activity projection. */
export function projectActivity(
  stageReceipts: Array<{ stage: string; status: string; detail?: string }> | undefined,
  requests: TaskRequestRow[] | undefined,
  isLive: boolean,
): EpisodeActivity {
  const receipts = Array.isArray(stageReceipts) ? stageReceipts : [];
  const lastReceipt = receipts.length ? receipts[receipts.length - 1] : undefined;
  const rounds: ActivityRound[] = (Array.isArray(requests) ? requests : [])
    .map((row, index) => {
      const attempts = Array.isArray(row.attempts) ? row.attempts : [];
      const tokens = attempts.reduce(
        (sum, a) => sum + (Number(a?.tokensIn) || 0) + (Number(a?.tokensOut) || 0), 0);
      return {
        roundNum: Number(row.roundNum ?? index + 1),
        turn: String(row.turn || ''),
        toolNames: Array.isArray(row.toolNames) ? row.toolNames.map(String) : [],
        tokens,
        running: false,
      };
    })
    .sort((a, b) => a.roundNum - b.roundNum);
  const hasRounds = rounds.length > 0;
  if (isLive && hasRounds) rounds[rounds.length - 1].running = true;

  let stage: string;
  if (lastReceipt && lastReceipt.stage) stage = lastReceipt.stage;
  else if (hasRounds) stage = copy('stageWorking');
  else stage = copy('stageStarting');
  const stageDone = !!lastReceipt && ['done', 'success', 'complete', 'pass']
    .includes(String(lastReceipt.status || '').toLowerCase());

  return { stage, stageDone: !isLive || stageDone, rounds, hasRounds };
}

function roundHtml(round: ActivityRound): string {
  const tools = round.toolNames.length
    ? `<div class="evo-act-tools">${round.toolNames
        .map(name => `<span class="evo-act-tool">${esc(name)}</span>`).join('')}</div>`
    : `<div class="evo-act-tools evo-act-thinking">${esc(copy('stageThinking'))}</div>`;
  const tokens = round.tokens
    ? `<span class="evo-act-tokens">${round.tokens.toLocaleString()} tok</span>` : '';
  return `<div class="evo-act-round${round.running ? ' evo-act-live' : ''}">` +
    `<div class="evo-act-round-head">` +
    `<span class="evo-act-round-n">${esc(copy('roundN'))} ${round.roundNum}</span>` +
    (round.running ? `<span class="evo-act-dot" aria-hidden="true"></span>` : '') +
    tokens +
    `</div>${tools}</div>`;
}

/** Render the chat-like live transcript for one running episode. */
export function liveConversationHtml(goalStatement: string, activity: EpisodeActivity): string {
  const stageChip = activity.stageDone
    ? `<span class="evo-stage-chip evo-stage-done">${esc(copy('stageDone'))}</span>`
    : `<span class="evo-stage-chip"><span class="evo-act-dot" aria-hidden="true"></span>${esc(activity.stage)}</span>`;
  const rounds = activity.rounds.length
    ? activity.rounds.map(roundHtml).join('')
    : `<div class="evo-act-empty">${esc(copy('stageStartingHint'))}</div>`;
  return `<div class="evo-live">` +
    `<div class="evo-live-stream">` +
    `<div class="evo-msg evo-msg-user"><div class="evo-msg-body">${esc(goalStatement)}</div></div>` +
    `<div class="evo-msg evo-msg-assistant">` +
    `<div class="evo-live-stage">${stageChip}</div>` +
    `<div class="evo-act-rounds">${rounds}</div>` +
    `</div></div></div>`;
}
