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
  rawCost: number;
  quality: number;
  onFrontier: boolean;
}

export interface ParetoExclusion {
  modelId: string;
  label: string;
  quality: number;
}

export interface ParetoOptions {
  toUsd?: (value: number, currency: string) => number;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
const MARGIN = { top: 28, right: 44, bottom: 62, left: 68 };
export const MARKER_CLEARANCE_PX = 20;
const SPREAD_STEP_PX = 7;
const SPREAD_MAX_STEPS = 140;
const GOLDEN_ANGLE = 2.399963229728653;
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

/** USD blended cost for one Model, or null when it cannot be plotted. */
function modelRawCost(
  model: ModelCatalogRow,
  toUsd: (value: number, currency: string) => number,
): number | null {
  const blended = blendedModelCost(model.pricing);
  if (blended === null) return null;
  const currency = String(model.pricing?.currency || 'USD').toUpperCase();
  const rawCost = toUsd(blended, currency);
  return Number.isFinite(rawCost) && rawCost >= 0 ? rawCost : null;
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
      const rawCost = modelRawCost(model, toUsd);
      if (rawCost === null) continue;
      points.push({
        modelId: `${model.creatorId}/${model.modelId}`,
        label: model.displayName,
        vendorLabel: group.label,
        brand: model.brand,
        rawCost,
        quality: intelligence,
        onFrontier: false,
      });
    }
  }
  const ordered = [...points].sort((left, right) => (
    left.rawCost - right.rawCost || right.quality - left.quality
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

export interface CostAxisDomain {
  logMin: number;
  logMax: number;
  hasPositive: boolean;
}

/**
 * Log10 x-axis domain from the positive raw costs. Free ($0) Models do not
 * stretch the log axis: they render in a dedicated lane at the left edge.
 */
export function costAxisDomain(points: readonly ParetoPoint[]): CostAxisDomain {
  const positive = points
    .map((point) => point.rawCost)
    .filter((cost) => cost > 0);
  if (!positive.length) return { logMin: -2, logMax: 1, hasPositive: false };
  return {
    logMin: Math.log10(Math.min(...positive)) - 0.04,
    logMax: Math.log10(Math.max(...positive)) + 0.04,
    hasPositive: true,
  };
}

/** Scored Models the chart cannot place because no official price is registered. */
export function buildParetoExclusions(
  groups: VendorGroup[],
  options: ParetoOptions = {},
): ParetoExclusion[] {
  const toUsd = options.toUsd ?? ((value: number) => value);
  const excluded: ParetoExclusion[] = [];
  for (const group of groups) {
    for (const model of group.models) {
      const intelligence = model.aa?.intelligence;
      if (intelligence === null || intelligence === undefined) continue;
      if (modelRawCost(model, toUsd) !== null) continue;
      excluded.push({
        modelId: `${model.creatorId}/${model.modelId}`,
        label: model.displayName,
        quality: intelligence,
      });
    }
  }
  return excluded.sort(
    (left, right) => right.quality - left.quality || left.label.localeCompare(right.label),
  );
}

export interface SpreadBounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

function clampValue(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/**
 * Deterministically fan out markers that would overlap. Non-colliding markers
 * keep their exact data position; displaced markers spiral around the anchor
 * and stay inside the plot bounds.
 */
export function spreadMarkers<T extends { x: number; y: number }>(
  items: readonly T[],
  bounds: SpreadBounds,
): T[] {
  const ordered = items
    .map((item, index) => ({ item, index }))
    .sort((left, right) => (
      left.item.x - right.item.x || left.item.y - right.item.y || left.index - right.index
    ));
  const placed: { x: number; y: number }[] = [];
  const spread = new Array<T>(items.length);
  for (const { item, index } of ordered) {
    let x = clampValue(item.x, bounds.minX, bounds.maxX);
    let y = clampValue(item.y, bounds.minY, bounds.maxY);
    const collides = () => placed.some(
      (other) => Math.hypot(other.x - x, other.y - y) < MARKER_CLEARANCE_PX,
    );
    if (collides()) {
      for (let step = 1; step <= SPREAD_MAX_STEPS; step += 1) {
        const radius = SPREAD_STEP_PX * Math.sqrt(step);
        const angle = step * GOLDEN_ANGLE;
        x = clampValue(item.x + radius * Math.cos(angle), bounds.minX, bounds.maxX);
        y = clampValue(item.y + radius * Math.sin(angle), bounds.minY, bounds.maxY);
        if (!collides()) break;
      }
    }
    placed.push({ x, y });
    spread[index] = { ...item, x, y };
  }
  return spread;
}

interface LabelBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

function labelWidth(label: string): number {
  let units = 0;
  for (const char of label) units += char.charCodeAt(0) > 0xff ? 2 : 1;
  return units * 5.2;
}

function boxesOverlap(left: LabelBox, right: LabelBox): boolean {
  return left.x0 < right.x1 && right.x0 < left.x1 && left.y0 < right.y1 && right.y0 < left.y1;
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
  // CSS transforms apply reliably to a <g> wrapper; an inner <svg> ignores
  // them in some engines, which silently killed the hover zoom.
  const zoom = document.createElementNS(SVG_NS, 'g');
  zoom.classList.add('stg-mc-pareto-glyph');
  zoom.appendChild(glyph);
  marker.appendChild(zoom);
}

function renderChart(points: ParetoPoint[]): SVGSVGElement {
  const width = 1240;
  const height = 660;
  const plotWidth = width - MARGIN.left - MARGIN.right;
  const plotHeight = height - MARGIN.top - MARGIN.bottom;
  const hasFree = points.some((point) => point.rawCost <= 0);
  const laneWidth = hasFree ? 64 : 0;
  const laneRight = MARGIN.left + laneWidth;
  const laneX = MARGIN.left + laneWidth / 2;
  const logWidth = MARGIN.left + plotWidth - laneRight;
  const domain = costAxisDomain(points);
  const qualities = points.map((point) => point.quality);
  let yMin = Math.max(0, Math.floor((Math.min(...qualities) - 2) / 5) * 5);
  let yMax = Math.ceil((Math.max(...qualities) + 2) / 5) * 5;
  if (yMin >= yMax) yMax = yMin + 10;
  const xLog = (cost: number) => laneRight
    + ((Math.log10(cost) - domain.logMin) / (domain.logMax - domain.logMin)) * logWidth;
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

  if (hasFree) {
    svg.appendChild(svgElement('rect', {
      x: String(MARGIN.left), y: String(MARGIN.top),
      width: String(laneWidth), height: String(plotHeight),
      class: 'stg-mc-pareto-lane',
    }));
    svg.appendChild(svgElement('line', {
      x1: String(laneRight), y1: String(MARGIN.top),
      x2: String(laneRight), y2: String(MARGIN.top + plotHeight),
      class: 'stg-mc-pareto-lane-divider',
    }));
    svg.appendChild(svgElement('text', {
      x: String(laneX), y: String(MARGIN.top + plotHeight + 22),
      class: 'stg-mc-pareto-tick', 'text-anchor': 'middle',
    }, '$0'));
  }
  if (domain.hasPositive) {
    for (let decade = Math.ceil(domain.logMin); decade <= Math.floor(domain.logMax); decade += 1) {
      const tickX = xLog(10 ** decade);
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
  }, hasFree
    ? '3:1 输入/输出混合成本 · USD / 1M tokens（log；左列为官方价 $0）'
    : '3:1 输入/输出混合成本 · USD / 1M tokens（log）'));
  svg.appendChild(svgElement('text', {
    class: 'stg-mc-pareto-axis-label', 'text-anchor': 'middle',
    transform: `translate(20 ${MARGIN.top + plotHeight / 2}) rotate(-90)`,
  }, 'AA Index / 模型质量分'));

  const anchors = points.map((point) => ({
    point,
    x: point.rawCost > 0 ? xLog(point.rawCost) : laneX,
    y: y(point.quality),
  }));
  const positioned = [
    ...spreadMarkers(anchors.filter(({ point }) => point.rawCost <= 0), {
      minX: MARGIN.left + 11,
      minY: MARGIN.top + 11,
      maxX: laneRight - 11,
      maxY: MARGIN.top + plotHeight - 11,
    }),
    ...spreadMarkers(anchors.filter(({ point }) => point.rawCost > 0), {
      minX: laneRight + 11,
      minY: MARGIN.top + 11,
      maxX: MARGIN.left + plotWidth - 9,
      maxY: MARGIN.top + plotHeight - 9,
    }),
  ];
  const labelBoxes: LabelBox[] = [];
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
    const size = point.onFrontier ? 17 : 13;
    const hitRadius = size / 2 + 9;
    const marker = svgElement('g', {
      transform: `translate(${pointX.toFixed(1)} ${pointY.toFixed(1)})`,
      class: `stg-mc-pareto-point${point.onFrontier ? ' is-frontier' : ''}`,
      tabindex: '0',
    });
    marker.appendChild(svgElement('title', {},
      `${point.label} · ${point.vendorLabel}\nAA/质量 ${Number(point.quality.toFixed(1))} · ${displayCost(point.rawCost)}/1M\n${point.modelId}`));
    appendBrandGlyph(marker, point.brand, size);
    marker.appendChild(svgElement('circle', {
      r: String(hitRadius), class: 'stg-mc-pareto-hit',
    }));
    if (!point.onFrontier) {
      marker.appendChild(svgElement('text', {
        y: String(-(size / 2 + 8)), 'text-anchor': 'middle',
        class: 'stg-mc-pareto-hover-label',
      }, point.label));
    }
    svg.appendChild(marker);
    if (point.onFrontier) {
      const labelX = pointX + 15;
      let labelY = pointY - 11;
      const width = labelWidth(point.label);
      for (let attempt = 0; attempt < 8; attempt += 1) {
        const box: LabelBox = {
          x0: labelX - 1, y0: labelY - 9, x1: labelX + width + 1, y1: labelY + 2,
        };
        if (!labelBoxes.some((other) => boxesOverlap(other, box))) {
          labelBoxes.push(box);
          break;
        }
        labelY += 13;
        if (attempt === 7) labelBoxes.push(box);
      }
      svg.appendChild(svgElement('text', {
        x: String(labelX), y: String(labelY),
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
  const exclusions = buildParetoExclusions(groups, options);
  if (exclusions.length) {
    const names = exclusions.slice(0, 5)
      .map((entry) => `${entry.label}（AA ${entry.quality.toFixed(1)}）`)
      .join('、');
    note.textContent += ` 另有 ${exclusions.length} 个模型有质量分但缺官方价格，未入图：${names}`
      + `${exclusions.length > 5 ? ' 等' : ''}，补登记 list_pricing 后自动出现。`;
  }
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
