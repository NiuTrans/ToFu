/** Timeline presentation only: validated contract data -> text DOM and numeric SVG geometry.
 * Called by renderer.ts; no resource loading, model styles, HTML or lifecycle state.
 */
import type { timeline } from './contract.generated';

type Text = (key: string, fallback: string) => string;
const SVG_NS = 'http://www.w3.org/2000/svg';

export function renderTimeline(doc: Document, block: timeline, text: Text): HTMLElement {
  const element = (tag: string, content = '') => {
    const node = doc.createElement(tag);
    node.textContent = content;
    return node;
  };
  const number = new Intl.NumberFormat(doc.documentElement.lang || 'en', { maximumSignificantDigits: 15 });
  const time = (value: number) => `${value > 0 ? '+' : ''}${number.format(value)} ${block.axis.unit}`;
  const short = (label: string, length: number) => {
    const chars = [...label];
    return chars.length > length ? chars.slice(0, length - 1).join('') + '…' : label;
  };
  const root = element('div');
  root.className = 'rich-timeline';
  const scroll = element('div');
  scroll.className = 'rich-timeline-scroll';
  scroll.tabIndex = 0;
  scroll.dataset.richControl = 'timeline';
  scroll.setAttribute('role', 'region');
  scroll.setAttribute('aria-label', block.title);
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.classList.add('rich-timeline-plot');
  // The adjacent table is the complete, non-visual equivalent (including unabridged labels).
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const shape = (tag: 'line' | 'rect' | 'text', attrs: Record<string, number | string>, label?: string) => {
    const node = doc.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (label !== undefined) node.textContent = label;
    svg.append(node);
    return node;
  };
  const left = 160, right = 780;
  const x = (value: number) => left + (value - block.axis.start) / (block.axis.end - block.axis.start) * (right - left);
  const axisY = 32 + block.markers.length * 24;
  let y = axisY + 32;
  const laneBounds = new Map<string, { start: number; end: number }>();
  for (const lane of block.lanes) {
    const start = y;
    // Pack adjacent intervals on one lane; overlapping intervals get separate tracks.
    const trackEnds: number[] = [];
    const tracks = new Map<number, number>();
    lane.intervals.map((interval, index) => ({ interval, index }))
      .sort((a, b) => a.interval.start - b.interval.start || a.index - b.index)
      .forEach(({ interval, index }) => {
        let track = trackEnds.findIndex(end => end <= interval.start);
        if (track < 0) track = trackEnds.length;
        trackEnds[track] = interval.end;
        tracks.set(index, track);
      });
    const height = Math.max(1, trackEnds.length) * 40;
    shape('rect', { x: left, y, width: right - left, height, class: 'rich-timeline-track' });
    const label = shape('text', { x: 0, y: y + 24, class: 'rich-timeline-lane-label' }, short(lane.label, 18));
    const title = doc.createElementNS(SVG_NS, 'title'); title.textContent = lane.label; label.append(title);
    for (const [index, interval] of lane.intervals.entries()) {
      const rowY = y + tracks.get(index)! * 40;
      const width = x(interval.end) - x(interval.start);
      shape('rect', { x: x(interval.start), y: rowY + 4, width, height: 32,
        rx: 4, class: `rich-timeline-interval rich-timeline-${lane.kind}` });
      // Omit labels that cannot fit; exact labels and times always remain in the table.
      const capacity = Math.floor(width / 9);
      if (capacity > 2) shape('text', { x: x(interval.start) + 4, y: rowY + 25,
        class: 'rich-timeline-interval-label' }, short(interval.label, capacity));
    }
    y += height;
    laneBounds.set(lane.id, { start, end: y });
    y += 16;
  }
  const ticks = new Set(Array.from({ length: 5 }, (_, index) => block.axis.start + (block.axis.end - block.axis.start) * index / 4));
  if (block.axis.start < 0 && block.axis.end > 0) ticks.add(0);
  for (const tick of [...ticks].sort((a, b) => a - b)) {
    shape('line', { x1: x(tick), x2: x(tick), y1: axisY, y2: y - 16, class: 'rich-timeline-grid' });
    shape('text', { x: x(tick), y: axisY - 8, 'text-anchor': tick === block.axis.start ? 'start' : tick === block.axis.end ? 'end' : 'middle',
      class: 'rich-timeline-tick' }, time(tick));
  }
  for (const [index, marker] of block.markers.entries()) {
    const bounds = marker.laneId === undefined ? undefined : laneBounds.get(marker.laneId);
    shape('line', { x1: x(marker.time), x2: x(marker.time), y1: bounds?.start ?? axisY,
      y2: bounds?.end ?? y - 16, class: 'rich-timeline-marker' });
    shape('text', { x: x(marker.time), y: 16 + index * 24,
      'text-anchor': marker.time <= (block.axis.start + block.axis.end) / 2 ? 'start' : 'end',
      class: 'rich-timeline-marker-label' }, `${short(marker.label, 40)} · ${time(marker.time)}`);
  }
  svg.setAttribute('viewBox', `0 0 800 ${y}`);
  scroll.append(svg);
  root.append(scroll);

  const fallback = doc.createElement('details');
  fallback.className = 'rich-timeline-data';
  const disclosure = element('summary', text('rich.timelineData', 'Timeline data'));
  disclosure.dataset.richControl = 'timeline-data';
  fallback.append(disclosure);
  const wrap = element('div'); wrap.className = 'rich-table-scroll'; wrap.tabIndex = 0;
  wrap.dataset.richControl = 'timeline-table'; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', block.title);
  const table = doc.createElement('table');
  table.append(element('caption', `${block.title} · ${time(block.axis.start)} → ${time(block.axis.end)}`));
  const head = doc.createElement('thead'); const headings = doc.createElement('tr');
  [text('rich.timelineLane', 'Lane'), text('rich.timelineKind', 'Kind'), text('rich.timelineLabel', 'Label'),
    text('rich.timelineStart', 'Start'), text('rich.timelineEnd', 'End')].forEach(label => {
    const th = element('th', label); th.setAttribute('scope', 'col'); headings.append(th);
  });
  head.append(headings); table.append(head);
  const body = doc.createElement('tbody');
  const row = (lane: string, kind: string, label: string, start: string, end: string) => {
    const tr = doc.createElement('tr');
    const th = element('th', lane); th.setAttribute('scope', 'row'); tr.append(th);
    [kind, label, start, end].forEach(value => tr.append(element('td', value)));
    body.append(tr);
  };
  for (const lane of block.lanes) {
    if (!lane.intervals.length) row(lane.label, lane.kind, text('rich.timelineEmpty', 'No intervals'), '—', '—');
    for (const interval of lane.intervals) row(lane.label, lane.kind, interval.label, time(interval.start), time(interval.end));
  }
  for (const marker of block.markers) row(marker.laneId === undefined ? text('rich.timelineAllLanes', 'All lanes')
    : block.lanes.find(lane => lane.id === marker.laneId)!.label, text('rich.timelineMarker', 'Marker'), marker.label, time(marker.time), '—');
  table.append(body); wrap.append(table); fallback.append(wrap); root.append(fallback);
  return root;
}
