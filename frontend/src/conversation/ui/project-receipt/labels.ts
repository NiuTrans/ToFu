/** Human verbs and purpose captions. DOM-free projection over injected presentation ports. */
import type { ProjectReceiptRound, ProjectText } from "./contracts";

const PROJECT_RECEIPT_LABELS: Record<string, [string, string]> = {
  project_board_read: ["brainHead.boardRead", "Checked the team board"],
  project_charter_read: ["brainHead.charterRead", "Read the project charter"],
  project_charter_propose: ["brainHead.charterPropose", "Proposed a charter decision"],
  project_peer_status: ["brainHead.peerStatus", "Checked who else is working now"],
  project_feed_read: ["brainHead.feedRead", "Reviewed recent team activity"],
  project_message: ["brainHead.message", "Sent a note to another conversation"],
  project_intervene: ["brainHead.intervene", "Flagged an overlap to another conversation"],
  list_conversations: ["brainHead.listConvs", "Searched past conversations"],
  project_claim_path: ["brainHead.claimPath", "Reserved files for editing"],
  project_release_path: ["brainHead.releasePath", "Released a file reservation"],
  project_commit: ["brainHead.commit", "Committed this conversation's work"],
};

const PROJECT_RECEIPT_PURPOSES: Record<string, [string, string]> = {
  project_peer_status: ["brainWhy.peerStatus",
    "Sibling conversations of this project that are running right now — used to avoid duplicating work already in progress."],
  project_board_read: ["brainWhy.boardRead",
    "The shared to-do board across all conversations of this project — who is doing what, so work isn't duplicated."],
  project_feed_read: ["brainWhy.feedRead",
    "A recent timeline of what other conversations of this project have been doing."],
  project_charter_read: ["brainWhy.charterRead",
    "The project's shared goal and committed decisions that every conversation aligns to."],
  project_charter_propose: ["brainWhy.charterPropose",
    "Proposes a decision for the human to commit as shared project-wide intent — advisory until approved."],
  project_message: ["brainWhy.message",
    "An advisory note to a sibling conversation, delivered on its next turn — it never interrupts a running turn."],
  project_intervene: ["brainWhy.intervene",
    "Nudges a sibling conversation to re-check the board (advisory); a hard stop needs explicit human approval."],
  project_claim_path: ["brainWhy.claimPath",
    "Reserves specific files/paths on the shared board so sibling conversations hold off editing them while this conversation works — a durational, auto-expiring advisory lease, not a hard lock."],
  project_release_path: ["brainWhy.releasePath",
    "Clears a previously-held file/path reservation so sibling conversations may edit those paths again."],
  project_commit: ["brainWhy.commit",
    "Commits ONLY the files this conversation provably authored (byte-identical to its own last edit); files also carrying a sibling's uncommitted changes are held back, never swept in."],
  list_conversations: ["brainWhy.listConvs",
    "Searches your other conversations by title and content to find a relevant past discussion to reference."],
};

export function projectReceiptLabel(round: ProjectReceiptRound, text: ProjectText) {
  const _t = text;
  const tn = round.toolName || "";
  const raw = round.query || tn;

  if (tn.startsWith("project_board_") && !Object.hasOwn(PROJECT_RECEIPT_LABELS, tn)) {
    return _t("brainHead.boardMutate", "Updated the team board");
  }
  const entry = Object.hasOwn(PROJECT_RECEIPT_LABELS, tn) ? PROJECT_RECEIPT_LABELS[tn] : undefined;
  return entry ? _t(entry[0], entry[1]) : raw;
}

export function projectReceiptPurpose(round: ProjectReceiptRound, text: ProjectText) {
  const _t = text;
  const tn = round.toolName || "";

  if (tn.startsWith("project_board_") && !Object.hasOwn(PROJECT_RECEIPT_PURPOSES, tn)) {
    return _t("brainWhy.boardMutate",
      "Updates the shared to-do board so sibling conversations see this claim / change.");
  }
  const entry = Object.hasOwn(PROJECT_RECEIPT_PURPOSES, tn) ? PROJECT_RECEIPT_PURPOSES[tn] : undefined;
  return entry ? _t(entry[0], entry[1]) : "";
}
