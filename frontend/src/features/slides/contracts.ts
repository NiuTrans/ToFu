/** Slide editor ports and request identities. Server deck/patch data is authoritative. */
export interface SlideElement {
  elementId: string;
  elementType: string;
  text?: string;
  style?: { fontSize?: number | string; color?: string };
}
export type SlidePatch =
  | { op: 'set_text'; target: string; text: string }
  | { op: 'set_style'; target: string; props: { fontSize?: number; color?: string } };
export interface SlidesApi {
  pageFileUrl(taskId: string, page: number, revision: number): string;
  pageHtmlUrl(taskId: string, page: number): string;
  pageElements(taskId: string, page: number): Promise<{ elements?: SlideElement[] } | null>;
  patchPage(taskId: string, page: number, ops: SlidePatch[]): Promise<{
    ok?: boolean; errors?: string[]; detail?: string;
  } | null>;
}
/** Each page visit has a unique identity, even when task/page numbers repeat. No DOM. */
export interface PageVisit { readonly taskId: string; readonly page: number }
export type { Translator as Translate } from '../../i18n';
import type { Translator as Translate } from '../../i18n';
export interface EditorPorts {
  document: Document;
  getApi(): SlidesApi | null;
  translate: Translate;
  notifyApplied(): void;
  prefill(message: string): void;
}
