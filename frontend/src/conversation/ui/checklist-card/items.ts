/** Checklist item states and stable SVG vocabulary. Entry points below; dependencies are explicit imports and ports. */
import { escapeHtmlText as escapeValue } from "../../../html-safety";
import type { ChecklistItem, ChecklistText } from "./contracts";
const CHECKLIST_ICONS = {
    completed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>',
    in_progress: '<svg viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M12 3a9 9 0 1 0 9 9A9 9 0 0 0 12 3zm0 2v7l4.5 2.6A7 7 0 0 1 12 5z"/></svg>',
    blocked: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5"/><circle cx="12" cy="17" r=".7" fill="currentColor"/></svg>',
    pending: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/></svg>',
  };

export function checklistRows(todos: (ChecklistItem | null)[], text: ChecklistText): { html: string; done: number } {
  let rows = "";
  let done = 0;
  for (const item of todos) {
    const status = item?.status;
    if (status === "completed") done++;
    if (!item || typeof item !== "object") continue;
    const st = (status === "completed" || status === "in_progress" || status === "blocked") ? status : "pending";
    const itemText = escapeValue(String(item.content || "").trim());
    rows += `<div class="ptool-todo-item ptool-todo-${st}">` +
      `<span class="ptool-todo-mark">${CHECKLIST_ICONS[st]}</span>` +
      `<span class="ptool-todo-text">${itemText}</span>` +
      `</div>`;
  }
  if (!rows) {
    rows = `<div class="ptool-todo-empty">${escapeValue(text("todo.emptyBody", "No checklist items."))}</div>`;
  }
  return { html: rows, done };
}
