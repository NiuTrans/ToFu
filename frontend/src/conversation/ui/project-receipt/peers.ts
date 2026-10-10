/** Peer status, advisory delivery and charter review. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { PeerStatus, PeerDelivery, CharterProposal, ProjectText, ProjectReceiptPorts } from "./contracts";
export function peerStatusLabel(sl: string | undefined, text: ProjectText) {
  const _t = text;
  const s = String(sl || "").trim();
  if (!s) return "";
  if (s === "generating") return _t("projectBrain.stGenerating", "generating");
  if (s === "working") return _t("projectBrain.stWorking", "working");
  if (s === "idle") return _t("projectBrain.stIdle", "idle");
  let m: RegExpMatchArray | null;
  if ((m = s.match(/^editing\s+(.+)$/)))
    return _t("projectBrain.peerEditing", "editing {file}").replace("{file}", m[1]);
  if ((m = s.match(/^working\s+\((.+)\)$/)))
    return _t("projectBrain.stWorkingPhase", "working ({phase})").replace("{phase}", m[1]);
  return s;
}

export function renderPeerStatus(ps: PeerStatus | undefined, ports: ProjectReceiptPorts) {
  if (!ps) return "";
  const _t = ports.text;
  const peers = ps.peers || [];
  if (!peers.length) {
    return `<div class="ptool-peer-empty">${escapeValue(_t("projectBrain.peerNone", "No active peers right now."))}</div>`;
  }
  let html = '<div class="ptool-peer-list">';
  for (const p of peers) {
    const who = p.title || ("conv " + String(p.convId || "").slice(0, 8));
    const sub = p.agentId ? `<span class="ptool-peer-agent">${escapeValue("sub-agent " + p.agentId)}</span>` : "";
    const bits = [];
    if (p.statusLabel) bits.push(escapeValue(peerStatusLabel(p.statusLabel, _t)));
    if (p.round) bits.push(escapeValue(_t("projectBrain.peerRound", "round {n}").replace("{n}", String(p.round))));
    if (p.currentFile) bits.push(escapeValue(p.currentFile));
    const epic = p.claimedEpic
      ? `<div class="ptool-peer-epic">${ports.icon?.("package", 11) ?? ""}<span>${escapeValue(p.claimedEpic)}</span></div>` : "";
    html += `<div class="ptool-peer-card">` +
      `<div class="ptool-peer-who">${ports.icon?.("messageCircle", 12) ?? ""}<span>${escapeValue(who)}</span>${sub}</div>` +
      (bits.length ? `<div class="ptool-peer-detail">${bits.join(" · ")}</div>` : "") +
      epic + `</div>`;
  }
  html += "</div>";
  return html;
}

export function renderCharterProposal(cp: CharterProposal | undefined, ports: ProjectReceiptPorts) {
  if (!cp || !cp.proposal) return "";
  const _t = ports.text;
  const titleLine = cp.title
    ? `<div class="ptool-charter-prop-title">${escapeValue(cp.title)}</div>` : "";
  return `<div class="ptool-charter-proposal">` +
    titleLine +
    `<div class="ptool-charter-prop-text">${escapeValue(cp.proposal)}</div>` +
    `<div class="ptool-charter-prop-pending">` +
    `${ports.icon?.("hourglass", 11) ?? ""}` +
    `<span>${escapeValue(_t("projectBrain.proposalPending", "Awaiting human review — commit or reject in the Project Brain panel"))}</span>` +
    `</div></div>`;
}

export function renderPeerDelivery(pd: PeerDelivery | undefined, ports: ProjectReceiptPorts) {
  if (!pd || !pd.toConv) return "";
  const _t = ports.text;
  const isIntervene = pd.tool === "project_intervene";
  const verb = isIntervene
    ? (pd.hardAbort ? _t("projectBrain.pdHardIntervene", "Hard intervention") : _t("projectBrain.pdIntervene", "Advisory intervention"))
    : _t("projectBrain.pdMessage", "Message");
  const outcomeLabel = _t("projectBrain.pdOutcome." + (pd.outcome || "delivered"),
    pd.outcome || "delivered");
  const arrow = ports.icon?.("chevronDown", 12) ?? "→";
  const body = (pd.text || "").trim();
  // Show the TARGET conversation by its human-readable title, not a raw id
  // (a bare `conv mradmzmd` is meaningless to the user). Falls back to a
  // localized "Untitled chat" via convTitleById; a short id still resolves by
  // unique prefix against the loaded conversation list.
  const _target = ports.conversationTitle?.(pd.toConv) ?? ("conv " + String(pd.toConv || "").slice(0, 8));
  return `<div class="ptool-peermsg ptool-peermsg-${escapeValue(pd.outcome || "delivered")}">` +
    `<div class="ptool-peermsg-head">` +
    `<span class="ptool-peermsg-verb">${escapeValue(verb)}</span>` +
    `<span class="ptool-peermsg-arrow">${arrow}</span>` +
    `<span class="ptool-peermsg-target" title="${escapeValue(String(pd.toConv || ""))}">${escapeValue(_target)}</span>` +
    `<span class="ptool-peermsg-outcome ptool-peermsg-outcome-${escapeValue(pd.outcome || "delivered")}">${escapeValue(outcomeLabel)}</span>` +
    `</div>` +
    (body ? `<div class="ptool-peermsg-text">${escapeValue(body)}</div>` : "") +
    `</div>`;
}
