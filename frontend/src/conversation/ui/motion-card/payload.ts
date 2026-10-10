/** Decode a handler JSON envelope once per projection; malformed content has no structured body. */
import type { MotionPayload, MotionRound } from "./contracts";

export function readMotionPayload(round: MotionRound): MotionPayload {
  const raw = round.toolContent;
  if (typeof raw !== "string" || !raw.trim()) return {};
  try { const d = JSON.parse(raw); return (d && typeof d === "object") ? d as MotionPayload : {}; }
  catch (e) { return {}; }
}

/* ── Per-tool structured bodies ─────────────────────────────────────── */
