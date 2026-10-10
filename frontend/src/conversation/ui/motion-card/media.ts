/** Render, probe, concat, narration and mux receipts. Projection only; no I/O or browser state. */
import type { MotionPayload, MotionText } from "./contracts";
import { detailRow, section, list, escapeValue } from "./markup";

export function renderRender(p: MotionPayload, text: MotionText): string {
  const ok = !!p.ok;
  let rows = "";
  if (ok) {
    rows += detailRow(text("motionRow.output", "Output"), p.output, true);
    if (p.render_time_s != null) rows += detailRow(text("motionRow.renderTime", "Render time"), `${p.render_time_s}s`, true);
    if (p.elapsed != null) rows += detailRow(text("motionRow.wall", "Wall clock"), `${p.elapsed}s`, true);
  } else {
    rows += detailRow(text("motionRow.category", "Failure kind"), p.category || "failed");
    if (p.detail) rows += `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(p.detail).slice(0, 600))}</div>`;
  }
  return rows;
}

export function renderProbe(p: MotionPayload, text: MotionText): string {
  const pr = p.probe;
  if (!pr || typeof pr !== "object" || !Object.keys(pr).length) {
    return `<div class="ptool-motion-li ptool-motion-err">${escapeValue(text("motionRow.probeFailed", "probe failed — not a readable media file"))}</div>`;
  }
  const res = (pr.width && pr.height) ? `${pr.width}×${pr.height}` : "";
  let rows = detailRow(text("motionRow.codec", "Codec"), pr.codec, true)
    + detailRow(text("motionRow.resolution", "Resolution"), res, true)
    + detailRow(text("motionRow.fps", "Frame rate"), pr.fps != null ? `${pr.fps} fps` : "", true)
    + detailRow(text("motionRow.duration", "Duration"), pr.duration != null ? `${Number(pr.duration).toFixed(2)}s` : "", true)
    + detailRow(text("motionRow.audioTrack", "Audio track"),
        pr.has_audio ? text("motionRow.audioYes", "present") : text("motionRow.audioNo", "silent"));
  return rows;
}

export function renderConcat(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    return detailRow(text("motionRow.category", "Failure kind"), p.category || "failed")
      + (p.detail ? `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(p.detail).slice(0, 600))}</div>` : "");
  }
  return detailRow(text("motionRow.output", "Output"), p.output, true)
    + detailRow(text("motionRow.duration", "Duration"), p.duration != null ? `${p.duration}s` : "", true);
}

export function renderNarrate(p: MotionPayload, text: MotionText): string {
  if (p.degraded) {
    return `<div class="ptool-motion-li ptool-motion-warn">${escapeValue(
      text("motionRow.narrateDegraded", "No TTS slot configured — the video ships silent."))}</div>`
      + (p.detail ? `<div class="ptool-motion-li">${escapeValue(p.detail)}</div>` : "");
  }
  const scenes = Array.isArray(p.scenes) ? p.scenes : [];
  const head = detailRow(text("motionRow.scenes", "Scenes"), String(scenes.length))
    + detailRow(text("motionRow.alignment", "Alignment"), p.alignment, true)
    + (p.overflow_total ? detailRow(text("motionRow.overflow", "Overflow"), `${p.overflow_total}s`, true) : "");
  if (!scenes.length) return head;
  const rows = scenes.map((s) => {
    const audio = s.audio_duration != null ? `${Number(s.audio_duration).toFixed(2)}s` : "—";
    const target = s.target_duration != null ? `${Number(s.target_duration).toFixed(2)}s` : "—";
    const grew = (s.target_duration != null && s.srt_duration != null
      && Number(s.target_duration) > Number(s.srt_duration) + 0.001);
    return `<div class="ptool-motion-scene${grew ? " ptool-motion-scene-grew" : ""}">`
      + `<span class="ptool-motion-scene-id">${escapeValue(s.scene_id)}</span>`
      + `<span class="ptool-motion-scene-dur">${escapeValue(text("motionRow.sceneDur", "audio {a} → scene {t}").replace("{a}", audio).replace("{t}", target))}</span>`
      + (s.overflow ? `<span class="ptool-motion-scene-overflow">+${escapeValue(s.overflow)}s</span>` : "")
      + `</div>`;
  }).join("");
  return head + section(text("motionSec.sceneDurations", "Per-scene durations"), rows);
}

export function renderMux(p: MotionPayload, text: MotionText): string {
  if (!p.ok) {
    return detailRow(text("motionRow.category", "Failure kind"), p.category || "failed")
      + (p.detail ? `<div class="ptool-motion-li ptool-motion-err">${escapeValue(String(p.detail).slice(0, 600))}</div>` : "");
  }
  return detailRow(text("motionRow.deliverable", "Deliverable"), p.output, true)
    + detailRow(text("motionRow.duration", "Duration"), p.duration != null ? `${p.duration}s` : "", true)
    + detailRow(text("motionRow.audioTrack", "Audio track"), text("motionRow.audioVerified", "verified present"));
}
