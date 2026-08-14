import { orchestrationRegistry } from './registry';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type Port = Record<string, unknown>;

export interface OrchestrationRequestReaderOptions {
  client?: Port | (() => Port | null) | null;
  report?: (context: string, result: unknown) => unknown;
}

export interface OrchestrationRequestReader {
  read(method: string, args?: unknown[]): Promise<Port>;
  report(context: string, result: unknown): boolean;
}

type RequestReaderWindow = Window & {
  createOrchestrationRequestReader?: typeof createOrchestrationRequestReader;
};

/** Invoke late-bound read ports and keep diagnostics outside business flow. */
export function createOrchestrationRequestReader(
  options: OrchestrationRequestReaderOptions = {},
): OrchestrationRequestReader {
  const client = (): Port | null => {
    const value = typeof options.client === 'function'
      ? options.client() : options.client;
    return value ?? null;
  };
  const read = async (method: string, args: unknown[] = []): Promise<Port> => {
    let result: unknown = null;
    try {
      const target = client();
      const operation = target?.[method];
      result = typeof operation === 'function'
        ? await (operation as (...values: unknown[]) => unknown).apply(
          target, Array.isArray(args) ? args : [])
        : null;
    } catch (error: unknown) {
      result = { ok: false, notFound: false, cause: error };
    }
    return result && typeof result === 'object'
      ? result as Port : { ok: false, notFound: false };
  };
  const report = (context: string, result: unknown): boolean =>
    reportOrchestrationDiagnostic(options.report, context, result);
  return Object.freeze({ read, report });
}

(orchestrationRegistry as unknown as RequestReaderWindow).createOrchestrationRequestReader =
  createOrchestrationRequestReader;
