/** WorkUnit presentation ports. Server snapshots own lifecycle; views own only mounted interaction state. */
import type { I18nKey } from '../../../i18n';
export type WorkText = (key: I18nKey, fallback: string) => string;
export interface Stage { name: string; state: string; attempt?: number; elapsedS?: number; lastErrors?: string[]; detail?: string }
export interface WorkUnit {
  kind: string; title?: string; status: string; convId?: string;
  stages?: Stage[]; stageTotal?: number;
  quality?: { degraded?: boolean; reason?: string };
  failure?: { message?: string; detail?: string; hint?: string };
  artifacts?: { label?: string; href?: string; pages?: number; bytes?: number }[];
  pages?: { total: number; rendered: number[]; rev?: Record<string, number>; href: string };
  pendingGate?: { guidanceId: string; title?: string; pages?: { page: number; pageType?: string; key_message?: string }[] };
  links?: { events?: string; abort?: string };
}
export interface Scene { scene_id: string; text?: string; content_duration_s?: number; has_video?: boolean }
export interface Operation { busy?: boolean; accepted?: boolean; error?: string }
export interface WorkState {
  transportError?: boolean; scenes?: Scene[]; scenesLoading?: boolean; scenesError?: boolean;
  operations: Map<string, Operation>;
}
export interface WorkUnitPorts {
  document: Document; text: WorkText;
  ownerId(): number | null;
  getSnapshot(taskId: string): Promise<unknown>;
  humanResponse(guidanceId: string, response: string): Promise<unknown>;
  abort(taskId: string): Promise<unknown>;
  scenes(taskId: string): Promise<unknown>;
  regenerate(taskId: string, sceneId: string): Promise<unknown>;
  resolveUrl(path: string): string;
  /** API registry supplies an already resolved URL, including any forwarded mount prefix. */
  sceneFileUrl(taskId: string, sceneId: string): string;
  openSlides(taskId: string, page: number, total: number): void;
  prefill(message: string): void;
  notify?(title: string, body: string): void;
}
export type WorkAction = { name: 'refresh' | 'abort' | 'scenes' | 'regenerate' | 'gate'; sceneId?: string; guidanceId?: string; response?: string };
export function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
export function readWorkUnit(value: unknown): WorkUnit | null {
  const unit = record(record(value)?.workUnit);
  return unit && typeof unit.kind === 'string' && typeof unit.status === 'string' ? unit as unknown as WorkUnit : null;
}
export function terminal(unit: WorkUnit): boolean { return ['done', 'degraded', 'failed', 'aborted'].includes(unit.status); }
export function ready(unit: WorkUnit): boolean { return unit.status === 'done' || unit.status === 'degraded'; }
export function phrase(text: WorkText, key: I18nKey, fallback: string, args: Record<string, unknown> = {}): string {
  return text(key, fallback).replace(/\{(\w+)\}/g, (match, name: string) => name in args ? String(args[name]) : match);
}
/** Only authenticated application resources, never executable or cross-origin payload URLs. */
export function resourcePath(value: unknown): string {
  return typeof value === 'string' && /^\/api\/v4\/[a-z]/.test(value) && !/[\\\s]/.test(value) ? value : '';
}
