/** Board snapshots and honest mutation outcomes. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { BoardSnapshot, BoardTransition, ProjectReceiptPorts } from "./contracts";
export function renderBoardSnapshot(snap: BoardSnapshot | undefined, ports: ProjectReceiptPorts) {
  if (!snap) return "";
  const _t = ports.text;
  const lanes = snap.lanes || {};
  const laneDef: ["open" | "claimed" | "done", string][] = [
    ["open", _t("projectBrain.laneOpen", "Open")],
    ["claimed", _t("projectBrain.laneClaimed", "In progress")],
    ["done", _t("projectBrain.laneDone", "Done")],
  ];
  let html = '<div class="ptool-board-mini">';
  for (const [key, label] of laneDef) {
    const epics = lanes[key] || [];
    const count = (snap[key] != null) ? snap[key] : epics.length;
    let cards = epics.map(function (e) {
      const owner = e.owner
        ? `<span class="ptool-board-mini-owner">${escapeValue(String(e.owner).slice(0, 8))}</span>` : "";
      const disp = e.dispatched
        ? `<span class="ptool-board-mini-auto" title="${escapeValue(_t("projectBrain.dispatchedTitle", "Started autonomously by the project brain"))}">${ports.icon?.("rocket", 10) ?? ""}</span>` : "";
      return `<div class="ptool-board-mini-card ptool-board-mini-${escapeValue(key)}"><span class="ptool-board-mini-title">${escapeValue(e.title || e.id || "")}</span>${owner}${disp}</div>`;
    }).join("");
    if (!cards) cards = '<div class="ptool-board-mini-empty">—</div>';
    html += `<div class="ptool-board-mini-lane"><div class="ptool-board-mini-head">${escapeValue(label)} <span class="ptool-board-mini-count">${escapeValue(count)}</span></div>${cards}</div>`;
  }
  html += "</div>";
  return html;
}

export function decodeBackendTitle(s: unknown) {
  return String(s == null ? "" : s)
    .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

export function renderBoardTransition(tr: BoardTransition | undefined, ports: ProjectReceiptPorts) {
  if (!tr || !tr.verb) return "";
  const _t = ports.text;
  // A mutation can FAIL by returning an error (board full, already-claimed,
  // task-not-found). The backend now carries `ok:false` + `error` so we render
  // an explicit failed card instead of a green "posted → open" that lies about
  // what happened (the reported bug: no visible failure, only in the raw text).
  const failed = tr.ok === false;
  const verbLabel = _t("projectBrain.boardVerb." + tr.verb, tr.verb);
  const rawTitle = (tr.title || "").trim();
  const titleHtml = rawTitle
    ? (ports.inlineMarkup?.(decodeBackendTitle(rawTitle)) ?? escapeValue(rawTitle))
    : `<span class="ptool-board-tr-untitled">${escapeValue(
        _t("projectBrain.boardUntitled", "(untitled epic)"))}</span>`;
  const idChip = tr.taskId
    ? `<span class="ptool-board-tr-id" title="${escapeValue(tr.taskId)}">${escapeValue(tr.taskId)}</span>`
    : "";
  const statusLabel = tr.status
    ? `<span class="ptool-board-tr-status ptool-board-mini-${escapeValue(tr.status)}">${escapeValue(_t("projectBrain.lane" + tr.status.charAt(0).toUpperCase() + tr.status.slice(1), tr.status))}</span>`
    : "";
  // On failure the "→ status" chip is replaced by a FAILED badge; the error
  // message gets its own prominent row so the user sees WHY without opening
  // the raw model text.
  const failBadge = failed
    ? `<span class="ptool-board-tr-failed">${ports.icon?.("alertTriangle", 12) ?? ""}<span>${escapeValue(_t("projectBrain.boardFailed", "failed"))}</span></span>`
    : "";
  const headRow = `<div class="ptool-board-tr-head">` +
    `<span class="ptool-board-tr-verb">${escapeValue(verbLabel)}</span>` +
    (failed
      ? failBadge
      : (tr.status ? `<span class="ptool-board-tr-arrow">${ports.icon?.("chevronDown", 12) ?? "→"}</span>${statusLabel}` : "")) +
    `</div>`;
  const titleRow = `<div class="ptool-board-tr-titlerow">` +
    `<span class="ptool-board-tr-title">${titleHtml}</span>${idChip}` +
    `</div>`;
  const errRow = (failed && (tr.error || "").trim())
    ? `<div class="ptool-board-tr-error">${escapeValue((tr.error || "").trim())}</div>`
    : "";
  const cls = failed ? "ptool-board-transition ptool-board-transition-failed" : "ptool-board-transition";
  return `<div class="${cls}">${headRow}${titleRow}${errRow}</div>`;
}
