export type RuntimeCallable = (...args: unknown[]) => unknown;
export function resolveRuntimeAction(name: string): RuntimeCallable | undefined;
export function getRuntimeService(name: string): unknown;
export function setRuntimeService(name: string, value: unknown): void;
export const runtimeReady: Promise<void>;
export function loadFeatureFlags(): Promise<void>;
