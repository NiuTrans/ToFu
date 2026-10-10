/** Predicate verdict normalization and shared countdown text. Entry points below; dependencies are explicit imports and ports. */
import type { TimerPoll, TimerText, TimerClockPorts } from "./contracts";
export function timerPollReason(p: TimerPoll, text: TimerText): string {
  const raw = p && p.reason ? String(p.reason) : "";
  // "predicate no match (exit=1)" / "predicate matched (exit=0)" — the pure
  // code/predicate verdict. Map to a plain ready/not-ready line + exit code.
  const m = raw.match(/^predicate (matched|no match) \(exit=(-?\d+)\)$/);
  if (m) {
    const isMatch = m[1] === "matched";
    const code = m[2];
    return isMatch
      ? text("timerBlock.predicateReady", "Condition met (command exit {code})").replace("{code}", code)
      : text("timerBlock.predicateWait", "Not met yet (command exit {code})").replace("{code}", code);
  }
  if (/^predicate ambiguous/.test(raw)) {
    return text("timerBlock.predicateAmbiguous", "Command result inconclusive — still waiting");
  }
  return raw;
}

export function timerNextPollText(nextTs: number, ports: TimerClockPorts): string {
  const _tf = ports.text;
  const secs = Math.max(0, Math.round((nextTs - ports.now()) / 1000));
  return secs > 0
    ? _tf("timerBlock.nextCheckIn", "Next check in ~{n}s…").replace("{n}", String(secs))
    : _tf("timerBlock.nextCheckNow", "Next check due now…");
}
