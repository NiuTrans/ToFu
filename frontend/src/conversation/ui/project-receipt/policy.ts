/** Routine-read disclosure and summary count policy. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { ProjectReceiptRound, ProjectReceiptMeta, ProjectText } from "./contracts";
const ROUTINE_PROJECT_READ_TOOLS = new Set([
  "project_peer_status", "project_board_read", "project_feed_read",
  "project_charter_read", "list_conversations",

]);

export function projectReceiptDefaultOpen(round: ProjectReceiptRound) {
  const tn = round.toolName || "";
  // Board MUTATIONS (post/claim/complete/block) are actions → open.
  if (tn.startsWith("project_board_") && tn !== "project_board_read") return true;
  return !ROUTINE_PROJECT_READ_TOOLS.has(tn);
}

export function projectReceiptSummaryChip(round: ProjectReceiptRound, meta: ProjectReceiptMeta, text: ProjectText) {
  const _t = text;
  const tn = round.toolName || "";
  let n: number | string | null = null, label = "";
  if (tn === "project_peer_status" && meta.peerStatus) {
    n = (meta.peerStatus.peers || []).length;
    label = _t("brainChip.peers", "{n} active").replace("{n}", String(n));
  } else if (tn === "project_board_read" && meta.boardSnapshot) {
    const snap = meta.boardSnapshot;
    n = (snap.open != null) ? snap.open : (snap.lanes && snap.lanes.open ? snap.lanes.open.length : 0);
    label = _t("brainChip.openEpics", "{n} open").replace("{n}", String(n));
  } else if (tn === "project_feed_read" && meta.feedActivity) {
    n = (meta.feedActivity.events || []).length;
    label = _t("brainChip.events", "{n} events").replace("{n}", String(n));
  } else {
    return "";
  }
  if (n == null) return "";
  return `<span class="ptool-convmeta-count">${escapeValue(label)}</span>`;
}
