/**
 * Responsibility: define the stateless clock, logging, and health-response
 * ports shared by browser availability monitors. This module owns no verdict,
 * timer, DOM node, subscription, or network request.
 */

export interface AvailabilityHealthProbeResponse {
  readonly ok?: boolean;
  readonly status?: number;
  json?(): Promise<unknown>;
}

export interface AvailabilitySchedule {
  now(): number;
  setTimeout(callback: () => void, delayMs: number): number;
  clearTimeout(handle: number): void;
  setInterval(callback: () => void, delayMs: number): number;
  clearInterval(handle: number): void;
}

export interface AvailabilityLogger {
  debug(message: string, ...details: readonly unknown[]): void;
  info(message: string, ...details: readonly unknown[]): void;
  warn(message: string, ...details: readonly unknown[]): void;
  error(message: string, ...details: readonly unknown[]): void;
}
