/** Verification instruction, command and cadence metadata. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { TIMER_CARD_POLICY } from "./contracts";
import type { TimerRound, TimerCardPorts } from "./contracts";
export function timerMetadata(round: TimerRound, isCodeTimer: boolean, ports: TimerCardPorts): string {
  const _t = ports.text;
  // ── What is being verified — show the check instruction + command so the
  //    user understands the timer's job, who runs it, and how often. ──
  //    The instruction can be long; render it expandable instead of clipping
  //    mid-sentence (the old slice(0,400) cut "report st…").
  let metaHtml = "";
  const instr = round._timerCheckInstruction || "";
  const cmd = round._timerCheckCommand || round._timerConditionCommand || "";
  const interval = round._timerPollInterval || 0;
  const maxPolls = round._timerMaxPolls || 0;
  if (instr || cmd || interval) {
    const cadence = interval
      ? (maxPolls
          ? _t("timerBlock.cadenceMax", "Checks every {n}s · up to {m} times").replace("{n}", String(interval)).replace("{m}", String(maxPolls))
          : _t("timerBlock.cadence", "Checks every {n}s").replace("{n}", String(interval)))
      : "";

    // Expandable instruction, rendered as Markdown (the model writes it in
    // Markdown). A collapsed max-height clamp reveals the full text on click —
    // the backend now ships the whole instruction, so "show more" shows all.
    let instrHtml = "";
    if (instr) {
      const LONG = instr.length > TIMER_CARD_POLICY.longInstructionChars;
      const valId = "tw-instr-" + round.roundNum;
      const bodyHtml = (ports.markdown)
        ? ports.markdown(instr)
        : escapeValue(instr);
      const moreTxt = _t("timerBlock.showMore", "show more");
      const lessTxt = _t("timerBlock.showLess", "show less");
      const toggle = LONG
        ? ' role="button" tabindex="0" aria-expanded="false" data-tofu-action="_timerInstructionToggle(this,event)" data-tofu-action-keydown="_timerInstructionToggle(this,event)"'
        : "";
      const moreLink = LONG
        ? `<span class="timer-meta-more" data-more="${escapeValue(moreTxt)}" data-less="${escapeValue(lessTxt)}">${escapeValue(moreTxt)}</span>` : "";
      instrHtml = `<div class="timer-meta-row timer-meta-row-instr"${toggle}>
        <span class="timer-meta-label">${escapeValue(_t("timerBlock.verifying", "Verifying"))}</span>
        <span class="timer-meta-val timer-meta-md md-content${LONG ? " timer-meta-clamp" : ""}" id="${escapeValue(valId)}">${bodyHtml}</span>
        ${moreLink}
      </div>`;
    }

    // Who decides the trigger. For a pure command-based (code) timer there is
    // NO model — the shell predicate decides. For LLM/hybrid the deciding model
    // is resolved AT EACH POLL and can differ between polls, so we describe the
    // tier here and let each poll line carry its own model chip (no misleading
    // single pinned model in the header).
    const deciderRow = isCodeTimer
      ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeValue(_t("timerBlock.decidedBy", "Decided by"))}</span><span class="timer-meta-val">${escapeValue(_t("timerBlock.deciderCode", "Shell command exit code — no model"))}</span></div>`
      : `<div class="timer-meta-row"><span class="timer-meta-label">${escapeValue(_t("timerBlock.verifier", "Verifier"))}</span><span class="timer-meta-val">${escapeValue(_t("timerBlock.verifierLLM", "Cheap LLM · resolved per poll (see each check below)"))}</span></div>`;

    const cmdLabel = isCodeTimer
      ? _t("timerBlock.predicate", "Predicate")
      : _t("timerBlock.command", "Command");

    metaHtml = `<div class="timer-watcher-meta">
      ${instrHtml}
      ${cmd ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeValue(cmdLabel)}</span><code class="timer-meta-cmd">${escapeValue(cmd.slice(0, TIMER_CARD_POLICY.commandPreviewChars))}</code></div>` : ""}
      ${deciderRow}
      ${cadence ? `<div class="timer-meta-row"><span class="timer-meta-label">${escapeValue(_t("timerBlock.cadenceLabel", "Cadence"))}</span><span class="timer-meta-val">${escapeValue(cadence)}</span></div>` : ""}
    </div>`;
  }


  return metaHtml;
}
