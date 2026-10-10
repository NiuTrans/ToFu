/** Activity chronology and hidden-self note. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { relativeReceiptTime } from "./time";
import type { FeedActivity, ProjectReceiptPorts } from "./contracts";
export function renderFeedActivity(fa: FeedActivity | undefined, ports: ProjectReceiptPorts) {
  if (!fa) return "";
  const _t = ports.text;
  const events = fa.events || [];
  if (!events.length) {
    const hiddenNote = Number(fa.hiddenSelf || 0) > 0
      ? ` ${escapeValue(_t("projectBrain.feedHiddenSelf", "{n} from this conversation hidden").replace("{n}", String(Number(fa.hiddenSelf))))}`
      : "";
    return `<div class="ptool-feed-empty">${escapeValue(_t("projectBrain.activityEmpty", "No activity yet"))}${hiddenNote}</div>`;
  }
  let html = '<div class="ptool-feed-list">';
  for (const ev of events) {
    const kind = ev.kind || "note";
    const kindLabel = _t("projectBrain.kind." + kind, kind);
    // Prefer the (backend-backfilled) title; else resolve the id to a title
    // via convTitleById — a raw `conv <id>` is meaningless to the user. Only
    // fall back to a short id when nothing resolves (conversation not loaded).
    const who = ev.title
      || (ev.convId ? ports.conversationTitle?.(ev.convId) ?? "conv " + String(ev.convId).slice(0, 8) : "");
    const when = relativeReceiptTime(ev.ts, ports);
    const summary = (ev.summary || "").trim();
    html += `<div class="ptool-feed-row ptool-feed-${escapeValue(kind)}">` +
      `<span class="ptool-feed-kind">${escapeValue(kindLabel)}</span>` +
      `<div class="ptool-feed-body">` +
      `<div class="ptool-feed-head">` +
      (who ? `<span class="ptool-feed-who">${escapeValue(who)}</span>` : "") +
      (when ? `<span class="ptool-feed-when">${escapeValue(when)}</span>` : "") +
      `</div>` +
      (summary ? `<div class="ptool-feed-summary">${escapeValue(summary)}</div>` : "") +
      `</div></div>`;
  }
  html += "</div>";
  // Own-conversation rows are filtered out backend-side (the model already
  // knows its own activity); say so in one muted line instead of rendering
  // them with a "this conversation" badge.
  const hiddenSelf = Number(fa.hiddenSelf || 0);
  if (hiddenSelf > 0) {
    html += `<div class="ptool-feed-hidden">${escapeValue(
      _t("projectBrain.feedHiddenSelf", "{n} from this conversation hidden")
        .replace("{n}", String(hiddenSelf)))}</div>`;
  }
  return html;
}
