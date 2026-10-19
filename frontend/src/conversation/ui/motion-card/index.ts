/** Multimedia receipt composition: one shell, explicit text and WorkUnit demand ports.
 * Content owners never import this facade. No DOM, listener, cache or network work.
 */
import type { MotionRound, MotionPayload, MotionText, MotionCardPorts } from './contracts';
import { MOTION_CARD_TITLES, MOTION_CARD_PURPOSES, MOTION_CARD_SOURCES } from './labels';
import { detailRow, escapeValue } from './markup';
import { readMotionPayload } from './payload';
import { renderEnvCheck, renderStoryboard, renderCheck } from './validation';
import { renderRender, renderProbe, renderConcat, renderNarrate, renderMux } from './media';
import { renderProduce, renderPatchSlides, renderEditVideo, renderRestyleSlides, renderRevert } from './production';
import { renderWorkUnitShell } from '../work-unit/shell';

const BODY_RENDERERS: Readonly<Record<string, (payload: MotionPayload, text: MotionText) => string>> = {
  motion_video_env_check: renderEnvCheck,
  motion_video_storyboard_check: renderStoryboard,
  motion_video_check: renderCheck,
  motion_video_render: renderRender,
  motion_video_probe: renderProbe,
  motion_video_concat: renderConcat,
  motion_video_narrate: renderNarrate,
  motion_video_mux: renderMux,
  produce_video: renderProduce,
  produce_report: renderProduce,
  produce_research: renderProduce,
  produce_slides: renderProduce,
  edit_video: renderEditVideo,
  patch_slides: renderPatchSlides,
  restyle_slides: renderRestyleSlides,
  revert_slides: renderRevert,
  revert_video: renderRevert,
};

export function renderMotionCard(round: MotionRound, svg: string, badgeHtml: string, ports: MotionCardPorts): string {
  const meta = (round.results || [])[0] || {};
  const text = ports.text;
  const tn = round.toolName || "";
  const headEntry = MOTION_CARD_TITLES[tn];
  const headLabel = headEntry ? text(headEntry[0], headEntry[1]) : (round.query || tn);
  const purposeEntry = MOTION_CARD_PURPOSES[tn];
  const purpose = purposeEntry ? text(purposeEntry[0], purposeEntry[1]) : "";
  const purposeHtml = purpose
    ? `<div class="ptool-convmeta-why">${escapeValue(purpose)}</div>` : "";
  const p = readMotionPayload(round);
  const bodyFn = BODY_RENDERERS[tn];
  const bodyInner = bodyFn ? bodyFn(p, text) : "";
  /* The call line ("Render scene-001 → scene-001.mp4 (draft)") is kept as the
   * last detail row — it's the model-facing label, still useful verbatim. */
  const callLine = (typeof round.query === "string" && round.query && round.query !== tn)
    ? detailRow(text("motionRow.call", "Call"), round.query, true) : "";
  const snippet = (tn.startsWith("motion_video_") ? "" : typeof meta.snippet === "string" ? meta.snippet : "");
  const snippetHtml = (!bodyInner && snippet)
    ? `<div class="ptool-motion-li">${escapeValue(snippet)}</div>` : "";
  if (!bodyInner && !snippetHtml && !callLine && !meta.workUnitRef?.taskId) return "";
  const emptyNote = (!bodyInner && !snippetHtml)
    ? `<div class="ptool-convmeta-empty">${escapeValue(text("tool.noContent", "No content returned."))}</div>` : "";
  const bodyHtml = `<div class="ptool-motion-body">${purposeHtml}${bodyInner}${snippetHtml}${emptyNote}${callLine}</div>`;
  const srcEntry = MOTION_CARD_SOURCES[tn];
  const srcLabel = srcEntry ? text(srcEntry[0], srcEntry[1]) : text("motionSrc", "Video");
  /* WorkUnit re-attach handle (docs/contracts/API_CONTRACT.md §2.18): the acceptance
   * meta carries workUnitRef {taskId, kind}; the card renders the handle +
   * an empty live region, and ui/workunit_tracker.js hydrates it in place.
   * The handle is durable (persisted round meta), so a conversation
   * re-render re-arms the same registration via the scan below. */
  const wuRef = (meta && meta.workUnitRef && typeof meta.workUnitRef === "object")
    ? meta.workUnitRef : null;
  const wuTaskId = wuRef && wuRef.taskId ? String(wuRef.taskId) : "";
  if (wuTaskId) {
    ports.scanWorkUnits?.();
    const title = String(p.topic || p.direction || headLabel);
    return renderWorkUnitShell(wuTaskId, String(wuRef?.kind || ''), title, srcLabel, text,
      { icon: svg, details: bodyHtml, round: round.roundNum });
  }
  return `<details class="ptool-convmeta-block ptool-motion-block" data-rn="${escapeValue(String(round.roundNum))}">
       <summary class="ptool-line ptool-convmeta-header">
         <span class="ptool-icon">${svg}</span>
         <span class="ptool-text">${escapeValue(headLabel)}</span>
         <span class="ptool-convmeta-src">${escapeValue(srcLabel)}</span>
         ${badgeHtml}
       </summary>
       ${bodyHtml}
     </details>`;
}
