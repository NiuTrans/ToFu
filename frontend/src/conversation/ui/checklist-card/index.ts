/** Checklist disclosure shell and progress projection. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import { checklistRows } from "./items";
import { checklistDetails } from "./details";
import type { ChecklistRound, ChecklistText } from "./contracts";
export function renderChecklistCard(round: ChecklistRound, svg: string, controlsHtml: () => string, text: ChecklistText): string | null {
  const meta = (round.results || [])[0] || {};
  let todos = Array.isArray(meta.todos) ? meta.todos : null;
  // Fallback: some persisted/legacy rounds may carry the list on the round
  // itself rather than in the first result meta.
  if (!todos && Array.isArray(round.todos)) todos = round.todos;
  if (!todos) return null;

  const _t = text;
  const total = todos.length;
  const { html: rows, done } = checklistRows(todos, text);
  const pct = total ? Math.round((done / total) * 100) : 0;
  const allDone = total > 0 && done === total;
  const updateCount = Number(meta.todoDisplayUpdateCount ?? round.todoDisplayUpdateCount
    ?? meta.todoUpdateCount ?? (meta.todoRejected || meta.todoNoop ? 0 : 1));

  const headLabel = meta.todoRejected
    ? _t("todo.rejected", "Update rejected")
    : meta.todoNoop
      ? _t("todo.unchanged", "Checklist unchanged")
      : total ? _t("todo.head", "Checklist") : _t("todo.cleared", "Checklist cleared");


  const barHtml = total
    ? `<span class="ptool-todo-minibar" role="progressbar" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100" title="${done}/${total}">` +
        `<span class="ptool-todo-minibar-fill${allDone ? " ptool-todo-minibar-done" : ""}" style="width:${pct}%"></span>` +
      `</span>`
    : "";
  const countChip = total
    ? `<span class="ptool-todo-count${allDone ? " ptool-todo-count-done" : ""}">${done}/${total}</span>`
    : "";
  const { revisionsChip, breadcrumbHtml, stateNotice, explanationHtml, historyHtml } = checklistDetails(round, meta, updateCount, text);

  return `<details class="ptool-todo-block" data-rn="${escapeValue(String(round.roundNum))}">
       <summary class="ptool-line ptool-todo-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeValue(headLabel)}</span>
         ${barHtml}
         ${countChip}
         ${controlsHtml()}
       </summary>
       <div class="ptool-todo-body">${explanationHtml}${breadcrumbHtml}${stateNotice}<div class="ptool-todo-list">${rows}</div>${revisionsChip}${historyHtml}</div>
     </details>`;
}
