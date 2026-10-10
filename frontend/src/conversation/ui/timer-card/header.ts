/** Timer state, identity and condition-tier header. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { TimerRound, TimerCardPorts } from "./contracts";
export function timerHeader(round: TimerRound, triggered: boolean, isActive: boolean, totalPolls: number, lastWasError: unknown, timerIdShort: string, isCodeTimer: boolean, condKind: string, ports: TimerCardPorts) {
  const _t = ports.text;
  const timerId = round._timerTimerId || "";
  // Header — the timer id is surfaced as a dedicated copyable chip (rendered
  // separately below), so the label text no longer embeds the raw id.
  let headerLabel, headerCls;
  const _idTxt = escapeValue(timerIdShort);
  const _s = (n: number) => (n !== 1 ? "s" : "");
  if (triggered) {
    headerLabel = _t("timerBlock.headTriggered", "Timer — triggered after {n} poll{s}")
      .replace("{n}", String(totalPolls)).replace("{s}", _s(totalPolls));
    headerCls = "timer-watcher-triggered";
  } else if (round._timerBackground) {
    headerLabel = _t("timerBlock.headBackground", "Timer — background watcher created; view Timer panel for current status");
    headerCls = "";
  } else if (round._timerOrphaned) {
    headerLabel = _t("timerBlock.headOrphaned", "Timer — task interrupted ({n} poll{s}, timer still active in background)")
      .replace("{n}", String(totalPolls)).replace("{s}", _s(totalPolls));
    headerCls = "timer-watcher-orphaned";
  } else if (isActive) {
    const skipN = round._timerSkipCount || 0;
    const skipSuffix = skipN > 0 ? _t("timerBlock.headSkipSuffix", ", {n} skipped").replace("{n}", String(skipN)) : "";
    const errSuffix = lastWasError ? _t("timerBlock.headErrSuffix", ", last check errored") : "";
    headerLabel = _t("timerBlock.headWatching", "Timer — watching… ({n} poll{s}{skip}{err})")
      .replace("{n}", String(totalPolls)).replace("{s}", _s(totalPolls))
      .replace("{skip}", skipSuffix).replace("{err}", errSuffix);
    headerCls = lastWasError ? "timer-watcher-active timer-watcher-warn" : "timer-watcher-active";
  } else {
    headerLabel = _t("timerBlock.headDone", "Timer — {status} ({n} poll{s})")
      .replace("{status}", round.status || "done")
      .replace("{n}", String(totalPolls)).replace("{s}", _s(totalPolls));
    headerCls = "";
  }
  // Dedicated copyable id chip — clicking it copies the FULL timer id to the
  // clipboard (handled by document delegation on `.timer-id-chip`), so the
  // long identifier is extracted out of the label into one prominent token.
  const idChip = timerId
    ? `<button type="button" class="timer-id-chip" data-timer-id="${escapeValue(timerId)}" title="${escapeValue(_t("timerBlock.idChipTitle", "Timer id — click to copy"))}"><span class="timer-id-txt">${_idTxt}</span>${ports.icon("clipboard", 10)}</button>`
    : "";
  // Distinct identity chip: a pure command-based (zero-LLM) timer vs a hybrid.
  const kindBadge = isCodeTimer
    ? `<span class="timer-kind-badge timer-kind-code" title="${escapeValue(_t("timerBlock.kindCodeTip", "Decided by a shell command — no model is called"))}">${escapeValue(_t("timerBlock.kindCode", "command-based"))}</span>`
    : (condKind === "hybrid"
        ? `<span class="timer-kind-badge timer-kind-hybrid" title="${escapeValue(_t("timerBlock.kindHybridTip", "Model verification is retained; a shell command supplies additional evidence"))}">${escapeValue(_t("timerBlock.kindHybrid", "hybrid"))}</span>`
        : "");


  return { headerLabel, headerCls, idChip, kindBadge };
}
