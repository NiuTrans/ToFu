/** Relative activity receipt times. DOM-free projection over injected presentation ports. */
import type { ProjectReceiptPorts } from "./contracts";
export function relativeReceiptTime(timestamp: unknown, ports: ProjectReceiptPorts): string {
  const value = Number(timestamp) || 0;
  if (!value) return "";
  const minutes = Math.floor(Math.max(0, Math.floor((ports.now() - value) / 1000)) / 60);
  if (minutes < 1) return ports.text("projectBrain.justNow", "just now");
  if (minutes < 60) return ports.text("projectBrain.minutesAgo", "{n}m ago").replace("{n}", String(minutes));
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return ports.text("projectBrain.hoursAgo", "{n}h ago").replace("{n}", String(hours));
  return ports.text("projectBrain.daysAgo", "{n}d ago").replace("{n}", String(Math.floor(hours / 24)));
}
