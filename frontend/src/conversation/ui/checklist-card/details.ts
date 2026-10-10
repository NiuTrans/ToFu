/** Checklist verdict, explanation, breadcrumbs and accepted revision history. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { ChecklistRound, ChecklistMeta, ChecklistText } from "./contracts";
export function checklistDetails(round: ChecklistRound, meta: ChecklistMeta, updateCount: number, text: ChecklistText) {
  const _t = text;
  const breadcrumbs = Array.isArray(meta.todoBreadcrumbs) ? meta.todoBreadcrumbs : [];
  const displayHistory = Array.isArray(meta.todoDisplayHistory) ? meta.todoDisplayHistory : [];
  const revisionsChip = updateCount > 1
    ? `<span class="ptool-todo-revisions">${escapeValue(_t("todo.updates", { n: updateCount }))}</span>`
    : "";
  const breadcrumbHtml = breadcrumbs.length > 1
    ? `<div class="ptool-todo-breadcrumbs">${breadcrumbs.map((b, i) =>
        `<span${i === breadcrumbs.length - 1 ? ' class="is-active"' : ''}>${escapeValue(b.label || b.checklist_id || "")}</span>`
      ).join('<i>›</i>')}</div>`
    : "";
  let stateNotice = "";
  if (meta.todoRejected) {
    // The explicit verdict owns the label; prose is detail, never a classifier.
    const reason = meta.todoRejectReason || round.toolContent || "";
    stateNotice = `<div class="ptool-todo-notice is-rejected">${escapeValue(_t("todo.rejected", "Update rejected"))}</div>` +
      `<pre class="ptool-todo-rejection-detail">${escapeValue(String(reason))}</pre>`;
  } else if (meta.todoNoop) {
    stateNotice = `<div class="ptool-todo-notice">${escapeValue(_t("todo.unchanged", "Checklist unchanged"))}</div>`;
  } else if (Array.isArray(meta.todoAutoPopped) && meta.todoAutoPopped.length) {
    stateNotice = `<div class="ptool-todo-notice is-restored">${escapeValue(_t("todo.parentRestored", "Child complete; returned to the parent checklist."))}</div>`;
  }
  const explanation = typeof meta.todoExplanation === "string" ? meta.todoExplanation.trim() : "";
  // Its own line, never a header flex sibling: an unstyled inline item
  // out-shrinks .ptool-text and collapses the label to a one-char column
  // (same failure as .ptool-error-reason, which lives on its own line too).
  const explanationHtml = explanation
    ? `<div class="ptool-todo-explanation">${escapeValue(explanation)}</div>` : "";
  const historyHtml = displayHistory.length > 1
    ? `<details class="ptool-todo-history"><summary>${escapeValue(_t("todo.history", { n: displayHistory.length }))}</summary><div>${displayHistory.map((h) => {
        const state = `${h.done}/${h.total}`;
        const rev = h.revision ? ` · r${h.revision}` : "";
        const operation = h.operation ? `<code>${escapeValue(h.operation)}</code>` : "";
        return `<span>${operation}${escapeValue(rev)}${operation || rev ? " · " : ""}${escapeValue(state)}</span>`;
      }).join("")}</div></details>`
    : "";
  return { revisionsChip, breadcrumbHtml, stateNotice, explanationHtml, historyHtml };
}
