/** Escaped detail rows, sections and bounded lists shared by receipt families. */
import { escapeHtml } from "../../../html-safety";

export function escapeValue(value: unknown) {
  return escapeHtml(String(value == null ? "" : value));
}

export function detailRow(label: string, value: unknown, mono = false) {
  if (value == null || value === "") return "";
  const cls = mono ? "ptool-motion-v ptool-motion-mono" : "ptool-motion-v";
  return `<div class="ptool-motion-row"><span class="ptool-motion-k">${escapeValue(label)}</span><span class="${cls}">${escapeValue(value)}</span></div>`;
}

export function section(title: string, innerHtml: string) {
  if (!innerHtml) return "";
  return `<div class="ptool-motion-section"><div class="ptool-motion-section-title">${escapeValue(title)}</div>${innerHtml}</div>`;
}

export function list(items: unknown, cls = "", max = 12) {
  if (!Array.isArray(items) || !items.length) return "";
  const cap = max || 12;
  const shown = items.slice(0, cap);
  const rows = shown.map((it) => `<div class="ptool-motion-li ${cls || ""}">${escapeValue(it)}</div>`).join("");
  const more = items.length > cap
    ? `<div class="ptool-motion-li ptool-motion-more">… +${items.length - cap} more</div>` : "";
  return rows + more;
}
