/** Timer receipt composition over authoritative recovery presentation. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { TIMER_CARD_POLICY } from "./contracts";
import { timerNextPollText } from "./labels";
import { timerHeader } from "./header";
import { timerMetadata } from "./metadata";
import { renderTimerPoll } from "./poll";
import type { TimerRound, TimerRecovery, TimerCardPorts, TimerPoll } from "./contracts";
export function renderTimerCard(round: TimerRound, recovered: TimerRecovery, controlsHtml: string, ports: TimerCardPorts): string {
  const receipt = round.results?.find(result => result.timerReceipt)?.timerReceipt;
  if (receipt) {
    round = { ...round, _timerTimerId: receipt.timerId,
      _timerBackground: receipt.background, _timerConditionKind: receipt.conditionKind,
      _timerCheckInstruction: receipt.checkInstruction,
      _timerCheckCommand: receipt.checkCommand, _timerConditionCommand: receipt.conditionCommand,
      _timerPollInterval: receipt.pollInterval, _timerMaxPolls: receipt.maxPolls };
  }
  const polls = recovered.polls;
  const triggered = recovered.triggered;
  const isActive = round.status === "searching" && !triggered && !round._timerBackground;
  const timerId = round._timerTimerId || "";
  let totalPolls = 0;
  let lastPoll: TimerPoll | null = null;
  for (const poll of polls) {
    if (poll.decision === "started") continue;
    totalPolls++;
    lastPoll = poll;
  }
  const timerIdShort = timerId ? timerId.slice(0, 12) : "";
  // Was the most recent poll a parse/LLM error? Surface it in the header so
  // a stuck verification (LLM not returning a usable decision) is obvious.
  const lastWasError = lastPoll && (lastPoll.decision === "error" || lastPoll.decision === "parse_error" || lastPoll.parseError);
  // Condition tier (backend `condition_kind`): a pure-code timer decides by a
  // shell predicate and NEVER calls an LLM — so it gets a distinct identity and
  // no "Verifier model" row. LLM/hybrid timers resolve a cheap model AT EACH
  // POLL, and that model can differ per poll (it shows on each poll line), so
  // we deliberately don't pin a single model in the header/meta.
  const _t = ports.text;
  const condKind = round._timerConditionKind
    || (round._timerCheckInstruction ? "llm"
        : (round._timerConditionCommand ? "code" : "llm"));
  const isCodeTimer = condKind === "code";

  const { headerLabel, headerCls, idChip, kindBadge } = timerHeader(round, triggered, isActive, totalPolls, lastWasError, timerIdShort, isCodeTimer, condKind, ports);

  const metaHtml = timerMetadata(round, isCodeTimer, ports);

  // ── "Next check in Ns" hint while active ──
  // The countdown text is refreshed in place every second by the 1 Hz
  // shared demand-scoped ticker (keyed on [data-timer-next]), so it
  // stays live without churning the fingerprint gate — mirrors the swarm
  // panel's [data-sw-start] approach.
  let nextPollHtml = "";
  const nextPollTs = Number(round._timerNextPollTs);
  if (isActive && Number.isFinite(nextPollTs) && nextPollTs > 0) {
    ports.demandCountdown?.();
    nextPollHtml = `<div class="timer-next-poll" data-timer-next="${nextPollTs}">${ports.icon('hourglass', 12)} <span class="timer-next-poll-txt">${escapeValue(timerNextPollText(nextPollTs, ports))}</span></div>`;
  }

  // Build poll lines (most recent first for readability)
  const MAX_VISIBLE = TIMER_CARD_POLICY.visiblePolls;
  const visible = polls.slice(-MAX_VISIBLE).reverse();
  const hidden = polls.length - MAX_VISIBLE;

  const pollLines = visible.map(poll => renderTimerPoll(poll, ports)).join("");

  let hiddenHtml = "";
  if (hidden > 0) {
    hiddenHtml = `<div class="timer-poll-hidden">${escapeValue(_t("timerBlock.hiddenChecks", "{n} earlier check{s} hidden").replace("{n}", String(hidden)).replace("{s}", hidden !== 1 ? "s" : ""))}</div>`;
  }

  // Skip heartbeat trailer — shows "N polls skipped (output unchanged)"
  //   so the user knows the timer is still alive even when the LLM isn't
  //   being called. Without this, long runs of identical check_command
  //   output look like the timer is frozen.
  let skipTrailer = "";
  if (round._timerSkipCount && isActive) {
    const skipTs = round._timerLastSkipTs
      ? ports.formatTime(round._timerLastSkipTs)
      : "";
    const lastPollNum = round._timerLastSkipPollNum || 0;
    skipTrailer = `<div class="timer-poll-line timer-poll-skipped">
      <span class="timer-poll-icon">${ports.icon('clock', 13)}</span>
      <span class="timer-poll-num">${lastPollNum ? escapeValue(`#${lastPollNum}`) : ""}</span>
      <span class="timer-poll-reason">${escapeValue(_t("timerBlock.skipped", "{n} poll{s} skipped — check_command output unchanged").replace("{n}", String(round._timerSkipCount)).replace("{s}", round._timerSkipCount !== 1 ? "s" : ""))}</span>
      <span class="timer-poll-meta">${escapeValue(skipTs)}</span>
    </div>`;
  }

  const uid = "tmr-r" + round.roundNum;
  const expandedByDefault = isActive;  // auto-expand while active
  return `<div class="timer-watcher-block ${headerCls}" data-rn="${escapeValue(String(round.roundNum))}">
       <div class="timer-watcher-header" role="button" tabindex="0" aria-expanded="${expandedByDefault ? 'true' : 'false'}" data-tofu-action="_timerWatcherToggle(this,event)" data-tofu-action-keydown="_timerWatcherToggle(this,event)">
         <span class="timer-watcher-icon icon-box">${ports.icon('timer', 13)}</span>
         ${idChip}
         <span class="timer-watcher-label">${escapeValue(headerLabel)}</span>
         ${kindBadge}
         ${isActive ? '<span class="ptool-spinner"></span>' : ''}
         ${controlsHtml}
         <span class="timer-toggle">${ports.icon('chevronDown', 14)}</span>
       </div>
       <div class="timer-watcher-body${expandedByDefault ? ' expanded' : ''}" id="${escapeValue(uid)}-wrap">
         ${metaHtml}${pollLines}${hiddenHtml}${skipTrailer}${nextPollHtml}
       </div>
     </div>`;
}
