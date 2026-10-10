/** Single poll outcome and expandable verification evidence. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { TIMER_CARD_POLICY } from "./contracts";
import { timerPollReason } from "./labels";
import type { TimerPoll, TimerCardPorts } from "./contracts";
export function renderTimerPoll(p: TimerPoll, ports: TimerCardPorts): string {
  const _t = ports.text;
  // Key both siblings by the backend poll identity, so new checks cannot inherit
  // a prior check's disclosure, focus or selection during reconciliation.
  const key = p.pollId ? `id:${p.pollId}` : p.pollNum != null ? `number:${p.pollNum}` : "";
  const identity = key ? ` data-prn="${escapeValue(key)}"` : "";
  let icon, cls, label;
  const isParseErr = p.decision === "parse_error" || p.parseError;
  if (p.decision === "started") {
    icon = ports.icon('bell', 13); cls = "timer-poll-started"; label = "";
  } else if (p.decision === "ready") {
    icon = ports.icon('save', 13); cls = "timer-poll-ready"; label = `#${p.pollNum}`;
  } else if (p.decision === "error") {
    icon = ports.icon('ban', 13); cls = "timer-poll-error"; label = `#${p.pollNum}`;
  } else if (isParseErr) {
    icon = ports.icon('zap', 13); cls = "timer-poll-error timer-poll-parse-err"; label = `#${p.pollNum}`;
  } else {
    icon = ports.icon('hourglass', 13); cls = "timer-poll-wait"; label = `#${p.pollNum}`;
  }
  const ts = p.ts ? ports.formatTime(p.ts) : "";
  // Plain, translated verdict for the visible line — a raw predicate note
  // ("predicate no match (exit=1)") becomes "Not met yet (command exit 1)".
  // A genuine LLM reason passes through unchanged.
  const fullReason = timerPollReason(p, _t);
  const reason = escapeValue(fullReason.slice(0, TIMER_CARD_POLICY.reasonPreviewChars));
  const tokens = p.tokensUsed ? ` · ${p.tokensUsed} tok` : "";
  // The raw LLM output — only meaningful (and only sent/persisted) when the
  // decision could not be parsed. This is the evidence that explains WHY the
  // poll failed, so it is the centerpiece of an errored poll's detail.
  const rawContent = (isParseErr && p.rawContent) ? String(p.rawContent) : "";
  // Per-poll model chip — which LLM made this decision.
  const modelChip = p.model
    ? `<span class="timer-poll-model" title="${escapeValue(_t("timerBlock.verifiedByTitle", "Verified by {model}").replace("{model}", p.model))}">${escapeValue(p.model)}</span>` : "";
  // Stable per-poll id (e.g. tmr_84bd4fb3.p36) — lets the user correlate this
  // exact check with the app.log line and the DB poll_log row.
  const pollIdChip = p.pollId
    ? `<span class="timer-poll-id" title="${escapeValue(_t("timerBlock.pollIdTitle", "Poll id — search app.log for this"))}">${escapeValue(p.pollId)}</span>` : "";
  // Per-poll tool-call timeline — reuse the swarm panel's .sw-tl-* look so
  // the timer's tool activity reads identically to a sub-agent's.
  const trace = Array.isArray(p.toolTrace) ? p.toolTrace : [];

  // Expandable detail: full reason + raw LLM output + tool-call timeline + check_command output.
  const hasDetail = (fullReason.length > TIMER_CARD_POLICY.reasonPreviewChars) || rawContent.length > 0 || trace.length > 0 || (p.cmdOutput && p.cmdOutput.length > 0);
  let detailHtml = "";
  if (hasDetail) {
    const fullReasonHtml = fullReason.length > TIMER_CARD_POLICY.reasonPreviewChars
      ? `<div class="timer-poll-detail-reason">${escapeValue(fullReason)}</div>` : "";
    // Raw LLM output — what the model actually returned when its decision
    // could not be parsed as JSON. Shown verbatim so the failure is diagnosable.
    const rawHtml = rawContent.length > 0
      ? `<div class="timer-poll-detail-label">${escapeValue(_t("timerBlock.rawOutput", "Raw LLM output (unparseable decision):"))}</div>` +
        `<pre class="timer-poll-detail-output timer-poll-raw"><code>${escapeValue(rawContent)}</code></pre>`
      : "";
    let traceHtml = "";
    if (trace.length > 0) {
      const rows = trace.map(tc => {
        const ticon = (tc.name ? ports.toolIcon?.(tc.name) : undefined) || ports.icon('wrench');
        const dot = tc.isError
          ? `<span class="sw-tl-dot sw-tl-failed">${ports.icon('x', 12)}</span>`
          : `<span class="sw-tl-dot sw-tl-done">${ports.icon('check', 12)}</span>`;
        const el = (typeof tc.elapsed === "number") ? `${tc.elapsed.toFixed(1)}s` : "";
        return `<div class="sw-tl-row sw-tl-${tc.isError ? "failed" : "done"}">
          <div class="sw-tl-line">${dot}<span class="sw-tl-icon">${ticon}</span>` +
          `<span class="sw-tl-name">${escapeValue(tc.name || "?")}</span>` +
          (tc.argsBrief ? `<span class="sw-tl-args" title="${escapeValue(tc.argsBrief)}">${escapeValue(tc.argsBrief)}</span>` : "") +
          (el ? `<span class="sw-tl-elapsed">${el}</span>` : "") +
        `</div></div>`;
      }).join("");
      traceHtml = `<div class="timer-poll-detail-label">${escapeValue(_t("timerBlock.toolsCalled", "Tools called this poll:"))}</div>` +
        `<div class="sw-a-timeline timer-poll-trace">${rows}</div>`;
    }
    const cmdOutHtml = (p.cmdOutput && p.cmdOutput.length > 0)
      ? `<div class="timer-poll-detail-label">${escapeValue(_t("timerBlock.checkOutput", "Check output (evidence):"))}</div><pre class="timer-poll-detail-output"><code>${escapeValue(p.cmdOutput)}</code></pre>`
      : "";
    detailHtml = `<div class="timer-poll-detail"${identity}>${fullReasonHtml}${rawHtml}${traceHtml}${cmdOutHtml}</div>`;
  }
  const toggleAttr = hasDetail
    ? ' role="button" tabindex="0" aria-expanded="false" data-tofu-action="_timerPollToggle(this,event)" data-tofu-action-keydown="_timerPollToggle(this,event)"'
    : "";
  const caret = hasDetail ? `<span class="timer-poll-caret">${ports.icon('chevronDown', 12)}</span>` : `<span class="timer-poll-caret-spacer"></span>`;
  const toolBadge = trace.length > 0
    ? `<span class="timer-poll-toolcount" title="${escapeValue(_t("timerBlock.toolCallsTitle", "{n} tool call(s) this poll").replace("{n}", String(trace.length)))}">${ports.icon('wrench', 11)} ${trace.length}</span>` : "";
  return `<div class="timer-poll-line ${cls}${hasDetail ? " timer-poll-has-detail" : ""}"${identity}${toggleAttr}>
      ${caret}
      <span class="timer-poll-icon">${icon}</span>
      <span class="timer-poll-num">${escapeValue(label)}</span>
      <span class="timer-poll-reason">${reason}</span>
      ${toolBadge}${pollIdChip}${modelChip}
      <span class="timer-poll-meta">${escapeValue(ts)}${escapeValue(tokens)}</span>
    </div>${detailHtml}`;
}
