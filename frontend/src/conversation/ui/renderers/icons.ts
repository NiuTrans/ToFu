/**
 * DOM glyphs shared by conversation blocks and turn actions. Unknown action
 * names remain label-only.
 * Entry points: documentAttachmentGlyph, disclosureChevron, lineSvgIcon,
 * actionIcon.
 * Dependencies: browser DOM primitives.
 */


/* 5-char cap mirrors the knowledge panel's _knowledgeFileGlyph. */
export function documentAttachmentGlyph(fileName: string): string {
  const extension = /\.([A-Za-z0-9]{1,5})$/.exec(fileName)?.[1];
  return extension ? extension.toUpperCase() : 'DOC';
}

export function disclosureChevron(document: Document, className: string): HTMLElement {
  const wrapper = document.createElement('span');
  wrapper.className = `${className} conversation-disclosure-chevron`;
  wrapper.setAttribute('aria-hidden', 'true');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'm5 6.5 3 3 3-3');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.5');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  svg.appendChild(path);
  wrapper.appendChild(svg);
  return wrapper;
}

export function lineSvgIcon(
  document: Document,
  pathData: ReadonlyArray<string>,
): SVGSVGElement {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  for (const data of pathData) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1.35');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
  }
  return svg;
}

/* Lucide-style 24px stroke glyphs for the turn action bar. Unknown actions
 * render label-only so a new action never breaks the bar. */
const ACTION_ICON_PATHS: Record<string, ReadonlyArray<string>> = {
  copy: [
    'M10 8h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2V10a2 2 0 0 1 2-2z',
    'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2',
  ],
  inspect: [
    'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z',
    'M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
  ],
  edit: [
    'M12 20h9',
    'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z',
  ],
  regenerate: [
    'M21 12a9 9 0 1 1-2.64-6.36L21 8',
    'M21 3v5h-5',
  ],
  resume: ['M6 4.5 19 12 6 19.5z'],
  translate: [
    'm5 8 6 6',
    'm4 14 6-6 2-3',
    'M2 5h12',
    'M7 2h1',
    'm22 22-5-10-5 10',
    'M14 18h6',
  ],
  export: [
    'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4',
    'm7 10 5 5 5-5',
    'M12 15V3',
  ],
  'promote-decision': [
    'M9 18h6',
    'M10 22h4',
    'M8.5 14.5A7 7 0 1 1 15.5 14.5C14.5 15.3 14 16 14 18h-4c0-2-0.5-2.7-1.5-3.5z',
  ],
  branch: [
    'M6 3v12',
    'M18 6m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
    'M6 18m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0',
    'M18 9a9 9 0 0 1-9 9',
  ],
  delete: [
    'M3 6h18',
    'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2',
    'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6',
    'M10 11v6',
    'M14 11v6',
  ],
};

export function actionIcon(
  document: Document,
  action: string,
): SVGSVGElement | null {
  const pathData = ACTION_ICON_PATHS[action];
  if (!pathData) return null;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  svg.classList.add('msg-action-icon');
  for (const data of pathData) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linecap', 'round');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
  }
  return svg;
}
