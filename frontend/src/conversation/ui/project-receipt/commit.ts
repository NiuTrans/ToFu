/** Commit plan, held files and verification outcomes. DOM-free projection over injected presentation ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { CommitResult, ProjectReceiptPorts } from "./contracts";
export function renderCommitResult(cr: CommitResult | undefined, ports: ProjectReceiptPorts) {
  if (!cr) return "";
  const _t = ports.text;
  const isPlan = cr.mode === "plan";
  const committed = cr.committed || [];
  const clean = cr.clean || [];
  const excluded = cr.excluded || [];
  // The "positive" set: committed files (real commit) or would-commit files (plan).
  const posFiles = (!isPlan && cr.ok) ? committed : clean;

  // ── Outcome chip: the single most important signal (committed / planned /
  //    failed / verify-mismatch). ──
  let outcome, outClass;
  if (!cr.ok) {
    outcome = _t("commitCard.outFailed", "not committed");
    outClass = "failed";
  } else if (isPlan) {
    outcome = _t("commitCard.outPlanned", "plan only");
    outClass = "planned";
  } else if (cr.verified === false) {
    outcome = _t("commitCard.outVerifyMismatch", "verify mismatch");
    outClass = "warn";
  } else {
    outcome = _t("commitCard.outCommitted", "committed");
    outClass = "committed";
  }
  const shaHtml = (cr.commitSha && !isPlan && cr.ok)
    ? `<span class="ptool-commit-sha" title="${escapeValue(_t("commitCard.shaTitle", "commit hash"))}">${escapeValue(String(cr.commitSha).slice(0, 12))}</span>`
    : "";

  let html = `<div class="ptool-commit ptool-commit-${escapeValue(outClass)}">`;
  html += `<div class="ptool-commit-head">` +
    `<span class="ptool-commit-outcome ptool-commit-outcome-${escapeValue(outClass)}">${escapeValue(outcome)}</span>` +
    shaHtml + `</div>`;

  if (!cr.ok && cr.error) {
    html += `<div class="ptool-commit-err">${escapeValue(cr.error)}</div>`;
  }

  // ── The clean / committed set. ──
  if (posFiles.length) {
    const headKey = (!isPlan && cr.ok) ? "commitCard.committedHead" : "commitCard.wouldCommitHead";
    const headDef = (!isPlan && cr.ok)
      ? "Committed ({n}) — provably yours"
      : "Would commit ({n}) — provably yours";
    html += `<div class="ptool-commit-group">` +
      `<div class="ptool-commit-grouphead ptool-commit-grouphead-clean">` +
      `${ports.icon?.("check", 11) ?? ""}` +
      `<span>${escapeValue(_t(headKey, headDef).replace("{n}", String(posFiles.length)))}</span></div>` +
      `<div class="ptool-commit-files">` +
      posFiles.map((p) => `<div class="ptool-commit-file">${escapeValue(p)}</div>`).join("") +
      `</div></div>`;
  } else if (cr.ok) {
    html += `<div class="ptool-commit-empty">${escapeValue(_t("commitCard.noneClean", "No files were provably attributable to this conversation."))}</div>`;
  }

  // ── The held-back / excluded set (each with its reason). ──
  if (excluded.length) {
    html += `<div class="ptool-commit-group">` +
      `<div class="ptool-commit-grouphead ptool-commit-grouphead-held">` +
      `${ports.icon?.("hourglass", 11) ?? ""}` +
      `<span>${escapeValue(_t("commitCard.heldHead", "Held back ({n}) — not committed").replace("{n}", String(excluded.length)))}</span></div>` +
      `<div class="ptool-commit-files">` +
      excluded.map(function (e) {
        const ns = e.numstat
          ? `<span class="ptool-commit-numstat">${escapeValue(e.numstat)}</span>` : "";
        const reason = e.reason
          ? `<span class="ptool-commit-reason">${escapeValue(e.reason)}</span>` : "";
        return `<div class="ptool-commit-file ptool-commit-file-held">` +
          `<span class="ptool-commit-fpath">${escapeValue(e.path || "")}</span>${ns}${reason}</div>`;
      }).join("") +
      `</div></div>`;
  }

  html += "</div>";
  return html;
}
