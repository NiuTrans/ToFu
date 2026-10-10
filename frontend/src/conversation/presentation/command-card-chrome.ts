/** Shared, pure chrome for shell and browser-JavaScript command cards.
 * Owns escaped inline descriptions and the established code/output actions;
 * interaction state and execution authority remain with the retained caller.
 */
import { escapeHtmlText } from '../../html-safety';
import type { Translator } from '../../i18n';
import { TOOL_ROUND_CHEVRON_RIGHT_SVG } from './tool-round-icons';


/** Icon + tool name already carry execution context; the label adds no information. */
export function commandCardContextHtml(kind: 'terminal' | 'browser', translate: Translator): string {
  void kind;
  void translate;
  return '';
}

export function commandCardDescriptionHtml(value: string): string {
  if (!value) return '';
  const escaped = escapeHtmlText(value);
  return `<span class="ptool-cmd-desc-inline" title="${escaped}">${escaped}</span>`;
}

export function commandCardFoldChipHtml(
  kind: 'cmd' | 'code' | 'output',
  open: boolean,
  translate: Translator,
): string {
  const label = translate(kind === 'output' ? 'toolCmd.foldOutput'
    : kind === 'code' ? 'toolBrowserExecution.foldCode' : 'toolCmd.foldCommand');
  const inner = `${TOOL_ROUND_CHEVRON_RIGHT_SVG}<span>${escapeHtmlText(label)}</span>`;
  const expanded = open ? 'true' : 'false';
  // Complete literal actions are required by the generated restricted registry.
  return kind === 'output'
    ? `<button type="button" class="ptool-cmd-fold ptool-cmd-fold-output" aria-expanded="${expanded}" data-tofu-action="_cmdOutputOpenToggle(this,event)">${inner}</button>`
    : `<button type="button" class="ptool-cmd-fold ptool-cmd-fold-cmd" aria-expanded="${expanded}" data-tofu-action="_cmdBodyToggle(this,event)">${inner}</button>`;
}
