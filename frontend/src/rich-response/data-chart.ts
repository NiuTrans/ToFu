/** Native data-chart presenter: validated generated contract -> safe numeric SVG + semantic table.
 * Entry: renderDataChart. No model HTML/SVG/styles, requests, listeners or retained state.
 * Central admission owns bounds and cross-field checks; the table retains input order/precision.
 */
import type { data_chart } from './contract.generated';

type Text = (key: string, fallback: string) => string;
const SVG_NS = 'http://www.w3.org/2000/svg';

/** Deterministic domains: optional zero baseline; constant domains expand symmetrically. */
export function chartDomain(values: number[], includeZero = false): [number, number] {
  let low = Infinity, high = -Infinity;
  for (const value of values) {
    if (value < low) low = value;
    if (value > high) high = value;
  }
  if (includeZero) { low = Math.min(0, low); high = Math.max(0, high); }
  if (low === high) {
    const padding = Math.max(1, Math.abs(low) * 0.05);
    low -= padding; high += padding;
  }
  return [low, high];
}

export function renderDataChart(doc: Document, block: data_chart, text: Text): HTMLElement {
  const element = (tag: string, content = '') => {
    const node = doc.createElement(tag); node.textContent = content; return node;
  };
  // String(number) is the lossless numeric equivalent; never round the semantic evidence.
  const value = (n: number, unit?: string) => `${String(n)}${unit ? ` ${unit}` : ''}`;
  const xLabel = block.xLabel ?? text('rich.chartX', 'X');
  const yLabel = block.yLabel ?? text('rich.chartValue', 'Value');
  const root = element('div'); root.className = 'rich-data-chart';
  const scroll = element('div'); scroll.className = 'rich-data-chart-scroll';
  scroll.tabIndex = 0; scroll.dataset.richControl = 'data-chart';
  scroll.setAttribute('role', 'region'); scroll.setAttribute('aria-label', block.title);
  const svg = doc.createElementNS(SVG_NS, 'svg');
  svg.classList.add('rich-data-chart-plot'); svg.setAttribute('viewBox', '0 0 800 420');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `${block.title} · ${text(`rich.chartKind.${block.kind}`, block.kind)}`);
  svg.setAttribute('focusable', 'false');
  const title = doc.createElementNS(SVG_NS, 'title'); title.textContent = block.title; svg.append(title);
  const shape = (tag: 'line' | 'rect' | 'circle' | 'path' | 'text', attrs: Record<string, string | number>, label?: string) => {
    const node = doc.createElementNS(SVG_NS, tag);
    for (const [key, val] of Object.entries(attrs)) node.setAttribute(key, String(val));
    if (label !== undefined) node.textContent = label;
    svg.append(node); return node;
  };
  const tooltip = (node: SVGElement, content: string) => {
    const title = doc.createElementNS(SVG_NS, 'title'); title.textContent = content; node.append(title);
  };
  const short = (label: string) => [...label].length > 18 ? [...label].slice(0, 17).join('') + '…' : label;
  const points = block.series.flatMap((series, seriesIndex) => series.points.map((point, pointIndex) => ({ series, seriesIndex, point, pointIndex })));
  const description = doc.createElementNS(SVG_NS, 'desc');
  description.textContent = text('rich.chartDescription', 'Complete values and labels are in the data table below.'); svg.append(description);
  if (block.kind === 'pie') {

    svg.classList.add('rich-data-chart-plot--pie');
    svg.setAttribute('viewBox', '225 20 350 360');
    const total = points.reduce((sum, { point }) => sum + point.value, 0);
    let angle = -Math.PI / 2;
    for (const [index, { point }] of points.entries()) {
      // Zero values remain in the table and legend but have no invented area.
      if (point.value === 0) continue;
      const sweep = point.value / total * Math.PI * 2;
      const next = angle + sweep;
      const paint = `rich-data-chart-mark rich-data-chart-paint-${index % 8}`;
      const node = sweep >= Math.PI * 2 - 1e-12
        ? shape('circle', { cx: 400, cy: 200, r: 150, class: paint })
        : shape('path', { d: `M 400 200 L ${400 + 150 * Math.cos(angle)} ${200 + 150 * Math.sin(angle)} A 150 150 0 ${sweep > Math.PI ? 1 : 0} 1 ${400 + 150 * Math.cos(next)} ${200 + 150 * Math.sin(next)} Z`, class: paint });
      tooltip(node, `${point.label}: ${value(point.value, block.yUnit)}`);
      const middle = angle + sweep / 2;
      // Slice ordinals match the full table, providing a non-color identifier.
      if (sweep >= 0.15) shape('text', { x: 400 + 105 * Math.cos(middle), y: 205 + 105 * Math.sin(middle), 'text-anchor': 'middle', class: 'rich-data-chart-slice-label' }, String(index + 1));
      angle = next;
    }
  } else {
    const left = 90, right = 770, top = 30, bottom = 340;
    const [yMin, yMax] = chartDomain(points.map(({ point }) => point.value), true);
    const [xMin, xMax] = block.kind === 'bar' ? [0, points.length] : chartDomain(points.map(({ point }) => point.x!));
    const x = (n: number) => left + (n - xMin) / (xMax - xMin) * (right - left);
    const y = (n: number) => bottom - (n - yMin) / (yMax - yMin) * (bottom - top);
    svg.dataset.xDomain = `${xMin},${xMax}`; svg.dataset.yDomain = `${yMin},${yMax}`;
    const ticks = new Set(Array.from({ length: 5 }, (_, i) => yMin + (yMax - yMin) * i / 4));
    ticks.add(0);
    for (const tick of [...ticks].sort((a, b) => a - b)) {
      shape('line', { x1: left, x2: right, y1: y(tick), y2: y(tick), class: tick === 0 ? 'rich-data-chart-axis' : 'rich-data-chart-grid' });
      shape('text', { x: left - 8, y: y(tick) + 4, 'text-anchor': 'end', class: 'rich-data-chart-tick' }, String(Number(tick.toPrecision(5))));
    }
    shape('text', { x: left, y: 18, class: 'rich-data-chart-label' }, short(`${yLabel}${block.yUnit ? ` (${block.yUnit})` : ''}`));
    shape('text', { x: (left + right) / 2, y: 410, 'text-anchor': 'middle', class: 'rich-data-chart-label' }, short(`${xLabel}${block.xUnit ? ` (${block.xUnit})` : ''}`));
    if (block.kind === 'bar') {
      const slot = (right - left) / points.length;
      points.forEach(({ point, series, seriesIndex }, index) => {
        const node = shape('rect', { x: left + slot * (index + 0.1), y: Math.min(y(0), y(point.value)), width: slot * 0.8,
          height: Math.abs(y(point.value) - y(0)), class: `rich-data-chart-mark rich-data-chart-paint-${seriesIndex}` });
        tooltip(node, `${series.label} · ${point.label}: ${value(point.value, block.yUnit)}`);
        // Bounded ordinal ticks avoid unreadable categorical labels at maximum density.
        if (points.length <= 16) shape('text', { x: left + slot * (index + 0.5), y: bottom + 24, 'text-anchor': 'middle', class: 'rich-data-chart-tick' }, points.length <= 8 ? short(point.label!) : String(index + 1));
      });
    } else {
      for (let i = 0; i < 5; i++) {
        const tick = xMin + (xMax - xMin) * i / 4;
        shape('text', { x: x(tick), y: bottom + 24, 'text-anchor': 'middle', class: 'rich-data-chart-tick' }, String(Number(tick.toPrecision(5))));
      }
      block.series.forEach((series, index) => {
        const ordered = series.points.map((point, pointIndex) => ({ point, pointIndex }))
          .sort((a, b) => a.point.x! - b.point.x! || a.pointIndex - b.pointIndex);
        if (block.kind === 'line') shape('path', { d: ordered.map(({ point }, i) => `${i ? 'L' : 'M'} ${x(point.x!)} ${y(point.value)}`).join(' '),
          class: `rich-data-chart-line rich-data-chart-paint-${index}`, 'stroke-dasharray': `${index + 2} ${index ? 3 : 0}` });
        ordered.forEach(({ point }) => {
          const node = shape('circle', { cx: x(point.x!), cy: y(point.value), r: 4 + index * 0.3, class: `rich-data-chart-mark rich-data-chart-paint-${index}` });
          tooltip(node, `${series.label} · ${point.label ?? ''} · ${value(point.x!, block.xUnit)}, ${value(point.value, block.yUnit)}`);
        });
        const last = ordered.at(-1)!.point;
        shape('text', { x: x(last.x!) - 8, y: y(last.value) - 8, 'text-anchor': 'end', class: 'rich-data-chart-label' }, String(index + 1));
      });
    }
  }
  scroll.append(svg); root.append(scroll);
  const legend = element('ol'); legend.className = 'rich-data-chart-legend';
  (block.kind === 'pie' ? points.map(({ point }) => point.label!) : block.series.map(series => series.label))
    .forEach(label => legend.append(element('li', label)));
  root.append(legend);
  const wrap = element('div'); wrap.className = 'rich-data-chart-table'; wrap.tabIndex = 0;
  wrap.dataset.richControl = 'data-chart-table'; wrap.setAttribute('role', 'region'); wrap.setAttribute('aria-label', block.title);
  const table = doc.createElement('table'); table.append(element('caption', `${block.title} · ${text('rich.chartData', 'Chart data')}`));
  const headings = element('tr');
  [text('rich.chartPoint', 'Point'), text('rich.chartSeries', 'Series'), text('rich.chartLabel', 'Label'), xLabel, yLabel].forEach(label => {
    const th = element('th', label); th.setAttribute('scope', 'col'); headings.append(th);
  });
  const head = element('thead'); head.append(headings); table.append(head);
  const body = element('tbody');
  points.forEach(({ series, point }, index) => {
    const row = element('tr'); const th = element('th', String(index + 1)); th.setAttribute('scope', 'row'); row.append(th);
    [series.label, point.label ?? '—', point.x === undefined ? '—' : value(point.x, block.xUnit), value(point.value, block.yUnit)]
      .forEach(content => row.append(element('td', content)));
    body.append(row);
  });
  table.append(body); wrap.append(table); root.append(wrap);
  return root;
}
