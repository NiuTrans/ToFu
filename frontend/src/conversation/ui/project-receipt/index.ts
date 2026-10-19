/** Project receipt composition and structured-body priority. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { projectReceiptLabel, projectReceiptPurpose } from "./labels";
import { projectReceiptDefaultOpen, projectReceiptSummaryChip } from "./policy";
import { renderBoardSnapshot, renderBoardTransition } from "./board";
import { renderPeerStatus, renderPeerDelivery, renderCharterProposal } from "./peers";
import { renderFeedActivity } from "./activity";
import { renderCommitResult } from "./commit";

import { renderConversationSearch, conversationSearchLabel } from "./search";
import type { ProjectReceiptRound, ProjectReceiptMeta, ProjectReceiptPorts } from "./contracts";
const PROJECT_RECEIPT_SOURCES: Record<string, [string, string]> = {
  Board: ["brainSrc.board", "Team board"],
  Charter: ["brainSrc.charter", "Charter"],
  Conversations: ["brainSrc.conversations", "Conversations"],
  ConvRef: ["brainSrc.conversations", "Conversations"],
  Peer: ["brainSrc.peer", "Team"],
  Git: ["brainSrc.git", "Git"],
};

function renderStructuredProjectBody(meta: ProjectReceiptMeta, ports: ProjectReceiptPorts): string {

  if (meta.conversationSearch) return renderConversationSearch(meta.conversationSearch, ports);
  if (meta.boardSnapshot) return renderBoardSnapshot(meta.boardSnapshot, ports);
  if (meta.boardTransition) return renderBoardTransition(meta.boardTransition, ports);
  if (meta.peerStatus) return renderPeerStatus(meta.peerStatus, ports);
  if (meta.feedActivity) return renderFeedActivity(meta.feedActivity, ports);
  if (meta.peerDelivery) return renderPeerDelivery(meta.peerDelivery, ports);
  if (meta.charterProposal) return renderCharterProposal(meta.charterProposal, ports);
  if (meta.commitResult) return renderCommitResult(meta.commitResult, ports);
  return "";
}

export function renderProjectReceipt(round: ProjectReceiptRound, svg: string, badgeHtml: string, ports: ProjectReceiptPorts): string {
  const meta = (round.results || [])[0] || {};
  const _t = ports.text;
  // project_commit is routed through the Board handler (source==="Board") but
  // it is a git operation, not a board action — give it its own source chip.
  const source = (round.toolName === "project_commit") ? "Git" : (meta.source || "");
  const srcEntry = Object.hasOwn(PROJECT_RECEIPT_SOURCES, source) ? PROJECT_RECEIPT_SOURCES[source] : undefined;
  const sourceLabel = srcEntry ? _t(srcEntry[0], srcEntry[1]) : (source || "");
  const sourceChip = sourceLabel && round.toolName !== "list_conversations"
    ? `<span class="ptool-convmeta-src">${escapeValue(sourceLabel)}</span>`
    : "";
  // Localized, plain-language header + a "why this ran / what it means"
  //   caption. Replaces the raw English backend display string (round.query)
  //   that the user found meaningless.
  const headLabel = round.toolName === "list_conversations"
    ? conversationSearchLabel(round, _t) : projectReceiptLabel(round, _t);
  const purpose = round.toolName === "list_conversations" ? "" : projectReceiptPurpose(round, _t);
  const purposeHtml = purpose
    ? `<div class="ptool-convmeta-why">${escapeValue(purpose)}</div>`
    : "";
  // Structured renderer first (driven off backend meta, not re-parsed prose).
  //   When a structured body is available it replaces the raw Markdown dump;
  //   otherwise we fall back to the full toolContent (charter_read text,
  //   peer message/intervene results, etc.).
  const structured = renderStructuredProjectBody(meta, ports);
  let bodyHtml;
  if (structured) {
    bodyHtml = `<div class="ptool-convmeta-structured">${structured}</div>`;
  } else {
    // Full content preferred; snippet is the mid-stream / pre-complete fallback.
    const content = (typeof round.toolContent === "string" && round.toolContent.trim())
      ? round.toolContent
      : (typeof meta.snippet === "string" ? meta.snippet : "");
    bodyHtml = content.trim()
      ? `<div class="ptool-convmeta-content md-content">${ports.markdown(content)}</div>`
      : `<div class="ptool-convmeta-empty">${escapeValue(_t("tool.noContent", "No content returned."))}</div>`;
  }
  // Default-collapse routine coordination READS (low-signal, fired
  //   constantly); keep MUTATING / DECISION cards open (they show an action
  //   the agent took). A collapsed read still carries an at-a-glance count
  //   chip in its summary ("3 peers active" / "5 open") so the user gets the
  //   signal without expanding.
  const isOpen = projectReceiptDefaultOpen(round);
  const openAttr = isOpen ? " open" : "";
  const countChip = isOpen ? "" : projectReceiptSummaryChip(round, meta, _t);
  return `<details class="ptool-convmeta-block"${openAttr} data-rn="${escapeValue(String(round.roundNum))}">
       <summary class="ptool-line ptool-convmeta-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeValue(headLabel)}</span>
         ${countChip}
         ${sourceChip}
         ${badgeHtml}
       </summary>
       <div class="ptool-convmeta-body">${purposeHtml}${bodyHtml}</div>
     </details>`;
}
