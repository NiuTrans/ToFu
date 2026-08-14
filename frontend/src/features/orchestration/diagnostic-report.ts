import { orchestrationRegistry } from './registry';
type DiagnosticReportWindow = Window & {
  reportOrchestrationDiagnostic?: typeof reportOrchestrationDiagnostic;
};

/** Invoke an observational reporter without changing the caller's outcome. */
export function reportOrchestrationDiagnostic(
  reporter: unknown,
  ...args: unknown[]
): boolean {
  if (typeof reporter !== 'function') return false;
  try {
    (reporter as (...values: unknown[]) => unknown).apply(null, args);
    return true;
  } catch {
    return false;
  }
}

(orchestrationRegistry as unknown as DiagnosticReportWindow).reportOrchestrationDiagnostic =
  reportOrchestrationDiagnostic;
