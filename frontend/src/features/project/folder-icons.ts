/**
 * Responsibility: stroke icons for the project folder picker surfaces.
 * Entry point: folderIcon. Dependencies: none; icons are built through the
 * DOM, so no markup string is ever parsed.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const FOLDER = 'M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z';

type Shape = readonly [tag: 'circle' | 'line' | 'path' | 'polygon' | 'polyline' | 'rect', attributes: Readonly<Record<string, string>>];

const ICONS = {
  up: [['polyline', { points: '18 15 12 9 6 15' }]],
  down: [['polyline', { points: '6 9 12 15 18 9' }]],
  home: [['path', { d: 'M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z' }]],
  folder: [['path', { d: FOLDER }]],
  folderCode: [['path', { d: FOLDER }], ['polyline', { points: '10 10.5 8 13 10 15.5' }], ['polyline', { points: '14 10.5 16 13 14 15.5' }]],
  folderPlus: [['path', { d: FOLDER }], ['line', { x1: '12', y1: '10', x2: '12', y2: '16' }], ['line', { x1: '9', y1: '13', x2: '15', y2: '13' }]],
  eye: [['path', { d: 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z' }], ['circle', { cx: '12', cy: '12', r: '3' }]],
  search: [['circle', { cx: '11', cy: '11', r: '7' }], ['line', { x1: '20.5', y1: '20.5', x2: '16.2', y2: '16.2' }]],
  close: [['line', { x1: '18', y1: '6', x2: '6', y2: '18' }], ['line', { x1: '6', y1: '6', x2: '18', y2: '18' }]],
  trash: [['polyline', { points: '3 6 5 6 21 6' }], ['path', { d: 'M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6' }], ['path', { d: 'M10 11v6M14 11v6' }], ['path', { d: 'M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2' }]],
  plus: [['line', { x1: '12', y1: '5', x2: '12', y2: '19' }], ['line', { x1: '5', y1: '12', x2: '19', y2: '12' }]],
  check: [['polyline', { points: '20 6 9 17 4 12' }]],
  chevron: [['polyline', { points: '9 18 15 12 9 6' }]],
  star: [['polygon', { points: '12 2.5 15 9 22 9.3 16.6 13.8 18.5 21 12 17 5.5 21 7.4 13.8 2 9.3 9 9' }]],
  lock: [['rect', { x: '4', y: '11', width: '16', height: '10', rx: '2' }], ['path', { d: 'M8 11V7a4 4 0 0 1 8 0v4' }]],
  pencil: [['path', { d: 'M12 20h9' }], ['path', { d: 'M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z' }]],
  locate: [['path', { d: FOLDER }], ['polyline', { points: '11 10.5 13.5 13 11 15.5' }]],
} as const satisfies Record<string, readonly Shape[]>;

export type FolderIconName = keyof typeof ICONS;

export function folderIcon(name: FolderIconName, ownerDocument: Document = document): SVGSVGElement {
  const svg = ownerDocument.createElementNS(SVG_NS, 'svg');
  for (const [attribute, value] of [
    ['viewBox', '0 0 24 24'], ['width', '16'], ['height', '16'], ['fill', 'none'],
    ['stroke', 'currentColor'], ['stroke-width', '2'], ['stroke-linecap', 'round'],
    ['stroke-linejoin', 'round'], ['aria-hidden', 'true'], ['focusable', 'false'],
  ]) svg.setAttribute(attribute, value);
  for (const [tag, attributes] of ICONS[name] as readonly Shape[]) {
    const shape = ownerDocument.createElementNS(SVG_NS, tag);
    for (const [attribute, value] of Object.entries(attributes)) shape.setAttribute(attribute, value);
    svg.append(shape);
  }
  return svg;
}
