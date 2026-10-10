/** Production, slide patch/restyle, edit and version receipts. Projection only; no I/O or browser state. */
import type { MotionPayload, MotionText } from "./contracts";
import { detailRow, section, list, escapeValue } from "./markup";

export function renderProduce(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    return `<div class="ptool-motion-li ptool-motion-err">${escapeValue(p.detail || text("motionRow.jobFailed", "job failed to start"))}</div>`;
  }
  let rows = "";
  if (p.task_id) rows += detailRow(text("motionRow.taskId", "Task"), String(p.task_id).slice(0, 20), true);
  if (p.topic) rows += detailRow(text("motionRow.topic", "Topic"), p.topic);
  if (p.direction) rows += detailRow(text("motionRow.direction", "Direction"), p.direction);
  if (p.visual_quality) rows += detailRow(text("motionRow.quality", "Quality"), p.visual_quality, true);
  if (p.depth) rows += detailRow(text("motionRow.depth", "Depth"), p.depth, true);
  if (p.deduped) rows += detailRow(text("motionRow.deduped", "Note"), text("motionRow.dedupedText", "joined the already-running job"));
  if (p.quality_hint) rows += `<div class="ptool-motion-li">${escapeValue(p.quality_hint)}</div>`;
  return rows;
}

export function renderPatchSlides(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    const errs = Array.isArray(p.errors) && p.errors.length ? p.errors : [p.detail];
    return errs.filter(Boolean).map((e) =>
      `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(e).slice(0, 300))}</div>`).join("");
  }
  let rows = "";
  const changes = Array.isArray(p.changes) ? p.changes : [];
  for (const c of changes.slice(0, 12)) {
    const label = `${c.op} ${c.target || c.page_target || ""}`;
    const delta = c.before || c.after
      ? ` — ${c.before || ""} → ${c.after || ""}` : "";
    rows += `<div class="ptool-motion-li">${escapeValue((label + delta).slice(0, 240))}</div>`;
  }
  if (changes.length > 12) rows += `<div class="ptool-motion-li ptool-motion-more">… +${changes.length - 12} more</div>`;
  return rows;
}

export function renderEditVideo(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    return `<div class="ptool-motion-li ptool-motion-err">${escapeValue(p.detail || text("motionRow.jobFailed", "job failed to start"))}</div>`;
  }
  let rows = "";
  if (p.scene_id) rows += detailRow(text("motionRow.scene", "Scene"), p.scene_id, true);
  if (p.edit_of) rows += detailRow(text("motionRow.editOf", "Video job"), String(p.edit_of).slice(0, 20), true);
  if (p.note) rows += `<div class="ptool-motion-li">${escapeValue(p.note)}</div>`;
  return rows;
}

export function renderRestyleSlides(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    const errs = Array.isArray(p.errors) && p.errors.length ? p.errors : [p.detail];
    let rows = errs.filter(Boolean).map((e) =>
      `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(e).slice(0, 300))}</div>`).join("");
    const avail = Array.isArray(p.available_themes) ? p.available_themes : [];
    if (avail.length) rows += `<div class="ptool-motion-li">${escapeValue(avail.join(", "))}</div>`;
    return rows;
  }
  const changes = Array.isArray(p.changes) ? p.changes : [];
  return changes.map((c) => {
    const after = (c.after && typeof c.after === "object")
      ? Object.entries(c.after).map(([k, v]) => `${k}=${v}`).join(" ")
      : String(c.after || "");
    return `<div class="ptool-motion-li">${escapeValue(`${c.op} → ${after}`.slice(0, 240))}</div>`;
  }).join("");
}

export function renderRevert(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    const errs = Array.isArray(p.errors) && p.errors.length ? p.errors : [p.detail];
    return errs.filter(Boolean).map((e) =>
      `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(e).slice(0, 300))}</div>`).join("");
  }
  let rows = "";
  if (p.version != null) rows += detailRow(text("motionRow.version", "Version"), `v${p.version}`, true);
  if (p.scene_id) rows += detailRow(text("motionRow.scene", "Scene"), p.scene_id, true);
  if (p.note) rows += `<div class="ptool-motion-li">${escapeValue(p.note)}</div>`;
  return rows;
}
