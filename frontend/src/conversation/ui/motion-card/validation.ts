/** Dependency, storyboard and static-gate receipts. Projection only; no I/O or browser state. */
import type { MotionPayload, MotionText } from "./contracts";
import { detailRow, section, list, escapeValue } from "./markup";

export function renderEnvCheck(p: MotionPayload, text: MotionText): string {
  const ok = !!p.ok;
  const statusLine = detailRow(
    text("motionRow.status", "Status"),
    ok ? text("motionRow.envReady", "ready — every dependency resolved") : text("motionRow.envMissing", "missing dependencies"),
  );
  const deps: string[] = [];
  const depRow = (name: string, val: unknown) => {
    const present = typeof val === "string" ? !!val : !!val;
    deps.push(`<div class="ptool-motion-dep ${present ? "ptool-motion-dep-ok" : "ptool-motion-dep-miss"}">`
      + `<span class="ptool-motion-dep-name">${escapeValue(name)}</span>`
      + `<span class="ptool-motion-dep-val">${present ? escapeValue(typeof val === "string" ? val : "ok") : text("motionRow.absent", "not found")}</span></div>`);
  };
  depRow("node", p.node);
  depRow("hyperframes", p.hyperframes);
  depRow("ffmpeg", p.ffmpeg);
  depRow("ffprobe", p.ffprobe);
  depRow("chrome", p.chrome);
  const issues = list(p.issues, "ptool-motion-warn");
  return statusLine + section(text("motionSec.deps", "Dependencies"), deps.join(""))
       + section(text("motionSec.issues", "Issues"), issues);
}

export function renderStoryboard(p: MotionPayload, text: MotionText): string {
  const span = Array.isArray(p.span_s) && p.span_s.length === 2
    ? `${p.span_s[0]}s → ${p.span_s[1]}s` : "";
  const rows = detailRow(text("motionRow.transcriptSpan", "Transcript span"), span, true)
    + detailRow(text("motionRow.errors", "Errors"), Array.isArray(p.errors) ? String(p.errors.length) : "0");
  const errs = list(p.errors, "ptool-motion-err");
  return rows + section(text("motionSec.problems", "Problems to fix"), errs);
}

export function renderCheck(p: MotionPayload, text: MotionText): string {
  const ok = !!p.ok;
  const gates = p.gates && typeof p.gates === "object" ? Object.keys(p.gates) : [];
  let rows = detailRow(text("motionRow.verdict", "Verdict"),
    ok ? text("motionRow.gatesPass", "all gates passed") : text("motionRow.gatesFail", "gate failed"), false);
  if (!ok && p.category) rows += detailRow(text("motionRow.category", "Failure kind"), p.category, true);
  if (p.elapsed != null) rows += detailRow(text("motionRow.elapsed", "Elapsed"), `${p.elapsed}s`, true);
  if (gates.length) rows += detailRow(text("motionRow.gates", "Gates run"), gates.join(", "));
  const errs = list(p.errors, "ptool-motion-err");
  const warns = list(p.warnings, "ptool-motion-warn");
  const hints = list(p.fix_hints, "ptool-motion-hint");
  return rows
    + section(text("motionSec.errors", "Errors"), errs)
    + section(text("motionSec.warnings", "Warnings"), warns)
    + section(text("motionSec.fixHints", "How to fix"), hints);
}
