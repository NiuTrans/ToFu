/**
 * Cost × Artificial Analysis Pareto chart for canonical Models.
 *
 * The chart is a read-only projection. Quality comes only from the external
 * AA enrichment keyed by Creator/Model; price comes from Model.list_pricing.
 */

import {
  MODEL_BRAND_COLORS,
  MODEL_BRAND_ICONS,
} from '../../core/model-brand-icons';
import { blendedModelCost } from './model';
import type { ModelCatalogRow, VendorGroup } from './types';

export interface ParetoPoint {
  modelId: string;
  label: string;
  vendorLabel: string;
  brand: string;
  cost: number;
  rawCost: number;
  quality: number;
  onFrontier: boolean;
}

export interface ParetoOptions {
  toUsd?: (value: number, currency: string) => number;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
const COST_FLOOR = 0.01;
const MARGIN = { top: 28, right: 44, bottom: 62, left: 68 };
let activeOverlay: HTMLElement | null = null;
let activeEscapeHandler: ((event: KeyboardEvent) => void) | null = null;

function svgElement<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attributes: Record<string, string>,
  text?: string,
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function displayCost(value: number): string {
  if (value === 0) return '$0';
  if (value < 0.01) return '<$0.01';
  if (value < 1) return `$${Number(value.toFixed(3))}`;
  return `$${Number(value.toFixed(2))}`;
}

function axisCost(value: number): string {
  return value >= 1 ? `$${Number(value.toFixed(1))}` : `$${Number(value.toPrecision(1))}`;
}

/** Project scored/priced Models and flag the increasing-cost frontier. */
export function buildParetoPoints(
  groups: VendorGroup[],
  options: ParetoOptions = {},
): ParetoPoint[] {
  const toUsd = options.toUsd ?? ((value: number) => value);
  const points: ParetoPoint[] = [];
  for (const group of groups) {
    for (const model of group.models) {
      const intelligence = model.aa?.intelligence;
      if (intelligence === null || intelligence === undefined) continue;
      const pricing = model.pricing;
      const blended = blendedModelCost(pricing);
      if (blended === null) continue;
      const currency = String(pricing?.currency || 'USD').toUpperCase();
      const rawCost = toUsd(blended, currency);
      if (!Number.isFinite(rawCost) || rawCost < 0) continue;
      points.push({
        modelId: `${model.creatorId}/${model.modelId}`,
        label: model.displayName,
        vendorLabel: group.label,
        brand: model.brand,
        cost: Math.max(rawCost, COST_FLOOR),
        rawCost,
        quality: intelligence,
        onFrontier: false,
      });
    }
  }
  const ordered = [...points].sort((left, right) => (
    left.cost - right.cost || right.quality - left.quality
  ));
  let bestQuality = -Infinity;
  for (const point of ordered) {
    if (point.quality > bestQuality) {
      point.onFrontier = true;
      bestQuality = point.quality;
    }
  }
  return points;
}

function appendBrandGlyph(marker: SVGGElement, brand: string, size: number): void {
  const source = MODEL_BRAND_ICONS[brand] ?? MODEL_BRAND_ICONS.generic;
  const holder = document.createElementNS(SVG_NS, 'g');
  holder.innerHTML = source;
  const glyph = holder.firstElementChild as SVGSVGElement | null;
  if (!glyph) return;
  glyph.setAttribute('x', String(-size / 2));
  glyph.setAttribute('y', String(-size / 2));
  glyph.setAttribute('width', String(size));
  glyph.setAttribute('height', String(size));
  const color = MODEL_BRAND_COLORS[brand] ?? MODEL_BRAND_COLORS.generic;
  glyph.setAttribute('color', color);
  if (!glyph.hasAttribute('fill')) glyph.setAttribute('fill', color);
  glyph.classList.add('stg-mc-pareto-glyph');
  marker.appendChild(glyph);
}

function renderChart(points: ParetoPoint[]): SVGSVGElement {
  const width = 1120;
  const height = 600;
  const plotWidth = width - MARGIN.left - MARGIN.right;
  const plotHeight = height - MARGIN.top - MARGIN.bottom;
  const costs = points.map((point) => point.cost);
  const qualities = points.map((point) => point.quality);
  let xMin = 10 ** (Math.floor(Math.log10(Math.min(...costs)) * 2) / 2);
  let xMax = 10 ** (Math.ceil(Math.log10(Math.max(...costs)) * 2) / 2);
  if (xMin >= xMax) {
    xMin /= Math.sqrt(10);
    xMax *= Math.sqrt(10);
  }
  let yMin = Math.max(0, Math.floor((Math.min(...qualities) - 3) / 5) * 5);
  let yMax = Math.ceil((Math.max(...qualities) + 3) / 5) * 5;
  if (yMin >= yMax) yMax = yMin + 10;
  const x = (cost: number) => MARGIN.left
    + ((Math.log10(cost) - Math.log10(xMin)) / (Math.log10(xMax) - Math.log10(xMin)))
      * plotWidth;
  const y = (quality: number) => MARGIN.top
    + (1 - (quality - yMin) / (yMax - yMin)) * plotHeight;

  const svg = svgElement('svg', {
    class: 'stg-mc-pareto-chart',
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': '模型成本与 AA Index / 质量分 Pareto 图',
  });
  svg.appendChild(svgElement('rect', {
    x: String(MARGIN.left), y: String(MARGIN.top),
    width: String(plotWidth), height: String(plotHeight),
    rx: '4', class: 'stg-mc-pareto-plot',
  }));

  for (let decade = Math.ceil(Math.log10(xMin)); decade <= Math.floor(Math.log10(xMax)); decade += 1) {
    const tickX = x(10 ** decade);
    svg.appendChild(svgElement('line', {
      x1: String(tickX), y1: String(MARGIN.top),
      x2: String(tickX), y2: String(MARGIN.top + plotHeight),
      class: 'stg-mc-pareto-grid',
    }));
    svg.appendChild(svgElement('text', {
      x: String(tickX), y: String(MARGIN.top + plotHeight + 22),
      class: 'stg-mc-pareto-tick', 'text-anchor': 'middle',
    }, axisCost(10 ** decade)));
  }
  const yStep = yMax - yMin <= 25 ? 5 : 10;
  for (let tick = yMin; tick <= yMax; tick += yStep) {
    const tickY = y(tick);
    svg.appendChild(svgElement('line', {
      x1: String(MARGIN.left), y1: String(tickY),
      x2: String(MARGIN.left + plotWidth), y2: String(tickY),
      class: 'stg-mc-pareto-grid',
    }));
    svg.appendChild(svgElement('text', {
      x: String(MARGIN.left - 12), y: String(tickY + 4),
      class: 'stg-mc-pareto-tick', 'text-anchor': 'end',
    }, String(tick)));
  }
  svg.appendChild(svgElement('text', {
    x: String(MARGIN.left + plotWidth / 2), y: String(height - 14),
    class: 'stg-mc-pareto-axis-label', 'text-anchor': 'middle',
  }, '3:1 输入/输出混合成本 · USD / 1M tokens（log）'));
  svg.appendChild(svgElement('text', {
    class: 'stg-mc-pareto-axis-label', 'text-anchor': 'middle',
    transform: `translate(20 ${MARGIN.top + plotHeight / 2}) rotate(-90)`,
  }, 'AA Index / 模型质量分'));

  const positioned = points.map((point) => ({ point, x: x(point.cost), y: y(point.quality) }));
  const frontier = positioned.filter(({ point }) => point.onFrontier).sort((a, b) => a.x - b.x);
  if (frontier.length > 1) {
    svg.appendChild(svgElement('path', {
      d: frontier.map(({ x: px, y: py }, index) => `${index ? 'L' : 'M'}${px.toFixed(1)} ${py.toFixed(1)}`).join(' '),
      class: 'stg-mc-pareto-frontier', fill: 'none',
    }));
  }

  const ordered = [...positioned].sort(
    (left, right) => Number(left.point.onFrontier) - Number(right.point.onFrontier),
  );
  for (const { point, x: pointX, y: pointY } of ordered) {
    const size = point.onFrontier ? 19 : 15;
    const marker = svgElement('g', {
      transform: `translate(${pointX.toFixed(1)} ${pointY.toFixed(1)})`,
      class: `stg-mc-pareto-point${point.onFrontier ? ' is-frontier' : ''}`,
      tabindex: '0',
    });
    marker.appendChild(svgElement('title', {},
      `${point.label} · ${point.vendorLabel}\nAA/质量 ${Number(point.quality.toFixed(1))} · ${displayCost(point.rawCost)}/1M\n${point.modelId}`));
    appendBrandGlyph(marker, point.brand, size);
    marker.appendChild(svgElement('circle', {
      r: String(size / 2 + 6), class: 'stg-mc-pareto-hit',
    }));
    svg.appendChild(marker);
    if (point.onFrontier) {
      svg.appendChild(svgElement('text', {
        x: String(pointX + 13), y: String(pointY - 10),
        class: 'stg-mc-pareto-point-label',
      }, point.label));
    }
  }
  return svg;
}

export function closeParetoDialog(): void {
  activeOverlay?.remove();
  activeOverlay = null;
  if (activeEscapeHandler) document.removeEventListener('keydown', activeEscapeHandler);
  activeEscapeHandler = null;
}

export function openParetoDialog(
  groups: VendorGroup[],
  options: ParetoOptions = {},
): HTMLElement | null {
  const points = buildParetoPoints(groups, options);
  if (points.length < 2) return null;
  closeParetoDialog();
  const overlay = document.createElement('div');
  overlay.className = 'stg-mc-pareto-overlay';
  const dialog = document.createElement('section');
  dialog.className = 'stg-mc-pareto-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', '成本与 AA Index');

  const head = document.createElement('header');
  head.className = 'stg-mc-pareto-head';
  head.innerHTML = '<div><strong>成本 × AA Index</strong>'
    + `<span>${points.length} 个同时具备质量分和价格的模型</span></div>`;
  const closeButton = document.createElement('button');
  closeButton.type = 'button';
  closeButton.className = 'stg-mc-pareto-close';
  closeButton.setAttribute('aria-label', '关闭图表');
  closeButton.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  closeButton.addEventListener('click', closeParetoDialog);
  head.appendChild(closeButton);

  const body = document.createElement('div');
  body.className = 'stg-mc-pareto-body';
  body.appendChild(renderChart(points));
  const note = document.createElement('p');
  note.className = 'stg-mc-pareto-note';
  note.textContent = '红线是 Pareto 前沿：在同等或更低成本下，没有更高质量分的模型。价格使用模型目录登记的官方 Model.list_pricing。';
  body.appendChild(note);
  dialog.append(head, body);
  overlay.appendChild(dialog);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) closeParetoDialog();
  });
  activeEscapeHandler = (event) => {
    if (event.key === 'Escape') closeParetoDialog();
  };
  document.addEventListener('keydown', activeEscapeHandler);
  document.body.appendChild(overlay);
  activeOverlay = overlay;
  closeButton.focus();
  return overlay;
}
