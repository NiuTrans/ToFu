/** Canvas citation map (Connected-Papers feel); real cites edges, never inferred. */
import { escapeHtml } from '../../../../html-safety';
import { knownNumber, paperIdentity } from '../state/literature';
import type { JsonObject, ResearchStream } from '../state/stream';

export const GRAPH_NODE_LIMIT = 200;
const MAX_EDGES = 800;
const MIN_YEAR = 1990;
const NOW_YEAR = new Date().getFullYear();
const GRAPH_PALETTE: [number, number, number][] = [
  [214, 230, 226], [163, 197, 192], [95, 158, 160], [13, 79, 85],
];

type Copy = (key: string) => string;
type Callbacks = {
  onOpen?: (paper: JsonObject) => void;
  onViewList?: () => void;
};

interface GraphNode extends JsonObject {
  id: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  radius: number;
  pinned?: boolean;
  color?: string;
  band?: number;
  degree?: number;
  _label?: boolean;
  _fade?: number;
}

const nodeIdentity = (node: GraphNode): string => String(node.id || '');

function paperLabel(paper: JsonObject): string {
  const title = String(paper.title || '').trim();
  if (!title) return paperIdentity(paper);
  const first = title.split(/\s+/)[0].replace(/[^A-Za-z][A-Za-z0-9]*/g, '');
  const year = knownNumber(paper.year);
  return (first.length >= 3 && /[A-Za-z]/.test(first) ? first : title.slice(0, 12))
    + (year != null ? ', ' + year : '');
}

function yearBand(year: number | null): number {
  if (year == null) return 1;
  const t = Math.max(0, Math.min(1, (year - MIN_YEAR) / Math.max(1, NOW_YEAR - MIN_YEAR)));
  return Math.min(GRAPH_PALETTE.length - 1, Math.floor(t * GRAPH_PALETTE.length));
}

function mix(a: number[], b: number[], t: number): string {
  const channel = (index: number) => Math.round(a[index] + (b[index] - a[index]) * t);
  return `rgb(${channel(0)},${channel(1)},${channel(2)})`;
}

export function graphNodes(papers: JsonObject[], edges: JsonObject[]): GraphNode[] {
  const rows = papers.slice(0, GRAPH_NODE_LIMIT)
    .filter(paper => paperIdentity(paper))
    .map((paper, index) => {
      const angle = index * 2.39996; // golden-angle scatter; the force pass relaxes it
      const spread = 60 + 30 * Math.sqrt(index);
      const citations = knownNumber(paper.citationCount);
      return {
        ...paper,
        id: paperIdentity(paper),
        x: Math.cos(angle) * spread,
        y: Math.sin(angle) * spread,
        vx: 0,
        vy: 0,
        radius: 5 + Math.sqrt(Math.min(60000, citations ?? 0)) / 10,
        band: yearBand(knownNumber(paper.year)),
        degree: 0,
        _fade: 0.35,
      } as GraphNode;
    });
  const byId = new Map(rows.map(node => [nodeIdentity(node), node]));
  const seen = new Set<string>();
  const valid = edges.filter(edge => {
    if (edge.relation !== 'cites' || edge.from === edge.to) return false;
    const from = byId.get(String(edge.from));
    const to = byId.get(String(edge.to));
    if (!from || !to) return false;
    const a = String(edge.from);
    const b = String(edge.to);
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (seen.has(key)) return false;
    seen.add(key);
    from.degree = (from.degree || 0) + 1;
    to.degree = (to.degree || 0) + 1;
    return true;
  }).slice(0, MAX_EDGES);
  rows.forEach(node => {
    node.radius = Math.max(5, Math.min(30, node.radius + Math.sqrt(node.degree || 0) * 1.5));
  });
  (rows as unknown as JsonObject[] & { _edges?: JsonObject[] })._edges = valid;
  return rows;
}

export function layoutTick(nodes: GraphNode[], edges: JsonObject[], iterations = 1): void {
  if (!nodes.length) return;
  const byId = new Map(nodes.map(node => [nodeIdentity(node), node]));
  const charge = 9000 / Math.max(1, nodes.length);
  for (let step = 0; step < iterations; step += 1) {
    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let distanceSquared = dx * dx + dy * dy;
        if (distanceSquared < 0.01) {
          dx = (i - j) * 0.01 + 0.02;
          dy = 0.01;
          distanceSquared = dx * dx + dy * dy;
        }
        const minimum = a.radius + b.radius + 16;
        const distance = Math.sqrt(distanceSquared);
        const force = distance < minimum
          ? Math.min(3, (minimum - distance) * 0.05)
          : Math.min(2.4, charge / distanceSquared);
        // Both regimes repel: dx points from a to b, so a steps back.
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }
    for (const edge of edges) {
      const a = byId.get(String(edge.from));
      const b = byId.get(String(edge.to));
      if (!a || !b) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const rest = a.radius + b.radius + 46;
      const force = (distance - rest) * 0.004;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
    for (const node of nodes) {
      node.vx = (node.vx - node.x * 0.004) * 0.82;
      node.vy = (node.vy - node.y * 0.004) * 0.82;
      if (!node.pinned) {
        node.x += node.vx;
        node.y += node.vy;
      }
      node._fade = Math.min(1, (node._fade ?? 0.35) + 0.02);
    }
  }
}

export class LiteratureGraph {
  private host: HTMLElement;
  private canvas: HTMLCanvasElement | null = null;
  private ctx: CanvasRenderingContext2D | null = null;
  private detail: HTMLElement | null = null;
  private legend: HTMLElement | null = null;
  private empty: HTMLElement | null = null;
  private nodes: GraphNode[] = [];
  private edges: JsonObject[] = [];
  private byId = new Map<string, GraphNode>();
  private neighbor = new Map<string, Set<string>>();
  private stream: ResearchStream | null = null;
  private copy: Copy = key => key;
  private callbacks: Callbacks = {};
  private camera = { x: 0, y: 0, k: 1 };
  private fitted = false;
  private hoverId = '';
  private selectedId = '';
  private dragNode: GraphNode | null = null;
  private panning = false;
  private pointer = { x: 0, y: 0, startX: 0, startY: 0, camX: 0, camY: 0, moved: false, id: -1 };
  private raf = 0;
  private running = false;
  private destroyed = false;
  private ink = '';
  private observer: ResizeObserver | null = null;
  private fitCount = 0;

  constructor(host: HTMLElement) {
    this.host = host;
  }

  private toWorld(event: PointerEvent | WheelEvent): { x: number; y: number } {
    const rect = this.canvas!.getBoundingClientRect();
    return {
      x: (event.clientX - rect.left - this.camera.x) / this.camera.k,
      y: (event.clientY - rect.top - this.camera.y) / this.camera.k,
    };
  }

  private toScreen(node: GraphNode): { x: number; y: number } {
    return {
      x: node.x * this.camera.k + this.camera.x,
      y: node.y * this.camera.k + this.camera.y,
    };
  }

  private nodeAt(event: PointerEvent): GraphNode | null {
    const point = this.toWorld(event);
    let best: GraphNode | null = null;
    let bestDistance = Infinity;
    for (const node of this.nodes) {
      const distance = Math.hypot(node.x - point.x, node.y - point.y);
      const hit = Math.max(node.radius, 10 / this.camera.k);
      if (distance < hit && distance < bestDistance) {
        best = node;
        bestDistance = distance;
      }
    }
    return best;
  }

  private fit(): void {
    if (!this.canvas || !this.nodes.length) return;
    const rect = this.canvas.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
    for (const node of this.nodes) {
      minX = Math.min(minX, node.x - node.radius);
      maxX = Math.max(maxX, node.x + node.radius);
      minY = Math.min(minY, node.y - node.radius);
      maxY = Math.max(maxY, node.y + node.radius);
    }
    const width = Math.max(60, maxX - minX);
    const height = Math.max(60, maxY - minY);
    this.camera.k = Math.max(0.08, Math.min(2.5,
      Math.min(rect.width / width, rect.height / height) * 0.9));
    this.camera.x = rect.width / 2 - (minX + width / 2) * this.camera.k;
    this.camera.y = rect.height / 2 - (minY + height / 2) * this.camera.k;
    this.fitted = true;
    this.fitCount = this.nodes.length;
  }

  private resize(): void {
    if (!this.canvas) return;
    const rect = this.host.getBoundingClientRect();
    const width = Math.max(200, Math.floor(rect.width));
    const height = Math.max(220, Math.floor(rect.height));
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    if (this.canvas.width !== Math.floor(width * ratio)
        || this.canvas.height !== Math.floor(height * ratio)) {
      this.canvas.width = Math.floor(width * ratio);
      this.canvas.height = Math.floor(height * ratio);
      this.canvas.style.width = width + 'px';
      this.canvas.style.height = height + 'px';
      if (!this.fitted && this.nodes.length) this.fit();
    }
    this.ctx?.setTransform(ratio, 0, 0, ratio, 0, 0);
  }

  private buildDom(): void {
    this.host.innerHTML = '<div class="rs-graph-shell">'
      + '<canvas class="rs-graph-canvas" role="img" tabindex="0"></canvas>'
      + '<div class="rs-graph-legend" hidden></div>'
      + '<div class="rs-graph-empty" hidden></div>'
      + '<div class="rs-graph-detail" hidden></div>'
      + '</div>';
    this.canvas = this.host.querySelector('canvas');
    this.ctx = this.canvas?.getContext('2d') ?? null;
    this.detail = this.host.querySelector('.rs-graph-detail');
    this.legend = this.host.querySelector('.rs-graph-legend');
    this.empty = this.host.querySelector('.rs-graph-empty');
    if (!this.canvas || !this.ctx) return;
    this.resize();
    this.observer = new ResizeObserver(() => {
      this.resize();
      this.draw();
    });
    this.observer.observe(this.host);
    this.canvas.addEventListener('pointerdown', event => this.onPointerDown(event));
    this.canvas.addEventListener('pointermove', event => this.onPointerMove(event));
    this.canvas.addEventListener('pointerup', event => this.onPointerUp(event));
    this.canvas.addEventListener('pointercancel', event => this.onPointerUp(event));
    this.canvas.addEventListener('pointerleave', () => {
      if (!this.dragNode && !this.panning) {
        this.hoverId = '';
        this.draw();
      }
    });
    this.canvas.addEventListener('wheel', event => {
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.0012);
      const rect = this.canvas!.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      const k = Math.max(0.15, Math.min(4, this.camera.k * factor));
      this.camera.x = px - (px - this.camera.x) * (k / this.camera.k);
      this.camera.y = py - (py - this.camera.y) * (k / this.camera.k);
      this.camera.k = k;
      this.draw();
    }, { passive: false });
    this.detail?.addEventListener('click', event => {
      const target = event.target as HTMLElement;
      if (target.closest('[data-graph-open]')) {
        const paper = this.byId.get(this.selectedId);
        if (paper) this.callbacks.onOpen?.(paper);
      } else if (target.closest('[data-graph-close]')) {
        this.selectedId = '';
        this.renderDetail();
        this.draw();
      } else if (target.closest('[data-graph-list]')) {
        this.callbacks.onViewList?.();
      }
    });
  }

  private onPointerDown(event: PointerEvent): void {
    if (!this.canvas) return;
    this.canvas.focus();
    const node = this.nodeAt(event);
    this.pointer = {
      x: event.clientX, y: event.clientY, startX: event.clientX, startY: event.clientY,
      camX: this.camera.x, camY: this.camera.y, moved: false, id: event.pointerId,
    };
    if (node) {
      this.dragNode = node;
      node.pinned = true;
    } else {
      this.panning = true;
    }
    this.canvas.setPointerCapture(event.pointerId);
    this.settle();
  }

  private onPointerMove(event: PointerEvent): void {
    if (!this.canvas) return;
    if (this.dragNode) {
      const point = this.toWorld(event);
      this.dragNode.x = point.x;
      this.dragNode.y = point.y;
      this.dragNode.vx = 0;
      this.dragNode.vy = 0;
      this.pointer.moved = true;
      this.settle();
      return;
    }
    if (this.panning) {
      this.camera.x = this.pointer.camX + (event.clientX - this.pointer.startX);
      this.camera.y = this.pointer.camY + (event.clientY - this.pointer.startY);
      if (Math.abs(event.clientX - this.pointer.startX)
          + Math.abs(event.clientY - this.pointer.startY) > 3) this.pointer.moved = true;
      this.draw();
      return;
    }
    const node = this.nodeAt(event);
    const id = node ? nodeIdentity(node) : '';
    if (id !== this.hoverId) {
      this.hoverId = id;
      this.canvas.style.cursor = id ? 'pointer' : 'grab';
      this.draw();
    }
  }

  private onPointerUp(event: PointerEvent): void {
    if (this.canvas?.hasPointerCapture?.(this.pointer.id)) {
      this.canvas.releasePointerCapture(this.pointer.id);
    }
    if (this.dragNode) {
      this.dragNode.pinned = false;
      if (!this.pointer.moved) {
        this.selectedId = this.selectedId === nodeIdentity(this.dragNode)
          ? '' : nodeIdentity(this.dragNode);
        this.renderDetail();
      }
      this.dragNode = null;
      this.settle();
      this.draw();
      return;
    }
    if (this.panning && !this.pointer.moved && this.selectedId) {
      this.selectedId = '';
      this.renderDetail();
      this.draw();
    }
    this.panning = false;
  }

  private focus(id: string): boolean {
    const active = this.hoverId || this.selectedId;
    if (!active) return true;
    return id === active || this.neighbor.get(active)?.has(id) === true;
  }

  private draw(): void {
    if (!this.ctx || !this.canvas) return;
    const rect = this.canvas.getBoundingClientRect();
    this.ctx.clearRect(0, 0, rect.width, rect.height);
    this.ctx.save();
    // World coordinates are origin-centered; the camera (fit/pan/zoom) maps them to the viewport.
    this.ctx.translate(this.camera.x, this.camera.y);
    this.ctx.scale(this.camera.k, this.camera.k);
    const active = this.hoverId || this.selectedId;
    this.ctx.lineWidth = Math.max(0.5, 0.9 / this.camera.k);
    for (const edge of this.edges) {
      const a = this.byId.get(String(edge.from));
      const b = byIdGet(this.byId, edge.to);
      if (!a || !b) continue;
      const highlighted = active
        && (String(edge.from) === active || String(edge.to) === active);
      this.ctx.strokeStyle = highlighted
        ? 'rgba(95,158,160,0.85)'
        : active ? 'rgba(120,134,132,0.10)' : 'rgba(120,134,132,0.26)';
      this.ctx.beginPath();
      this.ctx.moveTo(a.x, a.y);
      this.ctx.lineTo(b.x, b.y);
      this.ctx.stroke();
    }
    for (const node of this.nodes) {
      const id = nodeIdentity(node);
      const dimmed = !this.focus(id);
      this.ctx.globalAlpha = dimmed ? 0.18 : (node._fade ?? 1);
      this.ctx.fillStyle = node.color || '#5f9ea0';
      this.ctx.beginPath();
      this.ctx.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
      this.ctx.fill();
      if (id === active || id === this.selectedId) {
        this.ctx.globalAlpha = 1;
        this.ctx.strokeStyle = this.ink;
        this.ctx.lineWidth = 1.6 / this.camera.k;
        this.ctx.beginPath();
        this.ctx.arc(node.x, node.y, node.radius + 2.5 / this.camera.k, 0, Math.PI * 2);
        this.ctx.stroke();
      }
    }
    this.ctx.globalAlpha = 1;
    const showAll = this.camera.k > 0.55;
    const labelEvery = showAll ? 1 : this.camera.k > 0.3 ? 2 : 4;
    this.nodes.forEach((node, index) => {
      if (!node._label && index % labelEvery !== 0 && nodeIdentity(node) !== active) return;
      if (!this.focus(nodeIdentity(node))) return;
      this.ctx!.globalAlpha = Math.min(1, (node._fade ?? 1) + 0.1) * (node._label ? 1 : 0.75);
      this.ctx!.fillStyle = this.ink;
      this.ctx!.font = `${Math.max(9, 11 / this.camera.k)}px system-ui, sans-serif`;
      this.ctx!.textAlign = 'center';
      this.ctx!.fillText(paperLabel(node), node.x, node.y - node.radius - 5 / this.camera.k);
    });
    this.ctx.restore();
  }

  private settle(): void {
    if (this.running || this.destroyed || typeof window.requestAnimationFrame !== 'function') return;
    this.running = true;
    const frame = () => {
      if (this.destroyed) return;
      layoutTick(this.nodes, this.edges, 1);
      this.draw();
      let energy = 0;
      for (const node of this.nodes) energy = Math.max(energy, Math.abs(node.vx) + Math.abs(node.vy));
      if (energy > 0.05) {
        this.raf = window.requestAnimationFrame(frame);
      } else {
        this.running = false;
      }
    };
    this.raf = window.requestAnimationFrame(frame);
  }

  private renderDetail(): void {
    if (!this.detail) return;
    const node = this.byId.get(this.selectedId);
    if (!node) {
      this.detail.hidden = true;
      this.detail.innerHTML = '';
      return;
    }
    const copy = this.copy;
    const title = escapeHtml(String(node.title || paperIdentity(node)));
    const link = node.arxivId
      ? '<button type="button" class="rs-graph-detail-open" data-graph-open>' + title + ' ↗</button>'
      : node.doi
        ? '<a href="https://doi.org/' + encodeURIComponent(String(node.doi))
          + '" target="_blank" rel="noopener noreferrer">' + title + ' ↗</a>'
        : title;
    const meta: string[] = [];
    const year = knownNumber(node.year);
    if (year != null) meta.push(String(year));
    if (node.venue) meta.push(escapeHtml(String(node.venue)));
    const citations = knownNumber(node.citationCount);
    meta.push(citations != null
      ? escapeHtml(copy('citations') + ' ' + citations.toLocaleString())
      : escapeHtml(copy('citationsUnknown')));
    meta.push((node.degree || 0) + ' ' + escapeHtml(copy('graphEdges')));
    this.detail.innerHTML = '<div class="rs-graph-detail-head">'
      + '<div class="rs-graph-detail-title">' + link + '</div>'
      + '<button type="button" class="rs-graph-detail-close" data-graph-close aria-label="'
      + escapeHtml(copy('close')) + '">×</button></div>'
      + '<div class="rs-graph-detail-meta">' + meta.join(' · ') + '</div>'
      + (node.abstract
        ? '<p class="rs-graph-detail-abstract">' + escapeHtml(String(node.abstract).slice(0, 420)) + '</p>'
        : '')
      + '<button type="button" class="rs-graph-detail-list" data-graph-list>'
      + escapeHtml(copy('viewList')) + '</button>';
    this.detail.hidden = false;
  }

  private renderLegend(): void {
    if (!this.legend) return;
    const copy = this.copy;
    const dark = this.inkIsDark();
    const stops = GRAPH_PALETTE.map(color =>
      `rgba(${color[0]},${color[1]},${color[2]},${dark ? 0.95 : 1})`).join(',');
    this.legend.innerHTML = '<span>' + escapeHtml(copy('legendOld')) + '</span>'
      + '<i class="rs-graph-gradient" style="background:linear-gradient(90deg,' + stops + ')"></i>'
      + '<span>' + escapeHtml(copy('legendNew')) + '</span>'
      + '<span class="rs-graph-size-note">' + escapeHtml(copy('legendSize')) + '</span>';
    this.legend.hidden = false;
  }

  private inkIsDark(): boolean {
    const probe = getComputedStyle(this.host).getPropertyValue('--text-primary').trim();
    const match = probe.match(/#([0-9a-f]{6})/i) || probe.match(/rgb\((\d+),(\d+),(\d+)\)/);
    if (!match) return false;
    const values = match.length === 2
      ? [1, 3, 5].map(i => parseInt(match[1].slice(i, i + 2), 16))
      : match.slice(1).map(Number);
    return (values[0] * 299 + values[1] * 587 + values[2] * 114) / 1000 > 140;
  }

  update(stream: ResearchStream, copy: Copy, callbacks?: Callbacks): void {
    this.stream = stream;
    this.copy = copy;
    this.callbacks = callbacks || {};
    if (!this.canvas) this.buildDom();
    if (!this.canvas || !this.ctx) {
      this.host.innerHTML = '<p class="rs-graph-unavailable">'
        + escapeHtml(this.copy('graphUnavailable')) + '</p>';
      return;
    }
    this.ink = getComputedStyle(this.host).getPropertyValue('--text-primary').trim() || '#232529';
    const darkText = this.inkIsDark();
    const papers = (stream.papers || []).slice(0, GRAPH_NODE_LIMIT);
    const next = graphNodes(papers, stream.citationEdges || []);
    const edges = (next as unknown as JsonObject[] & { _edges?: JsonObject[] })._edges || [];
    const byId = new Map(this.nodes.map(node => [nodeIdentity(node), node]));
    for (const node of next) {
      const previous = byId.get(nodeIdentity(node));
      if (previous) {
        node.x = previous.x;
        node.y = previous.y;
        node.vx = previous.vx;
        node.vy = previous.vy;
        node._fade = previous._fade;
      }
      const base = GRAPH_PALETTE[node.band ?? 1];
      node.color = darkText ? mix(base, [24, 26, 28], 0.28) : `rgb(${base[0]},${base[1]},${base[2]})`;
    }
    this.nodes = next;
    this.edges = edges;
    this.byId = new Map(this.nodes.map(node => [nodeIdentity(node), node]));
    this.neighbor = new Map();
    for (const edge of edges) {
      const from = String(edge.from);
      const to = String(edge.to);
      if (!this.neighbor.has(from)) this.neighbor.set(from, new Set());
      if (!this.neighbor.has(to)) this.neighbor.set(to, new Set());
      this.neighbor.get(from)!.add(to);
      this.neighbor.get(to)!.add(from);
    }
    if (this.empty) {
      this.empty.hidden = this.nodes.length > 0;
      this.empty.textContent = this.nodes.length ? '' : this.copy('graphEmpty');
    }
    if (this.selectedId && !this.byId.has(this.selectedId)) {
      this.selectedId = '';
      this.renderDetail();
    } else if (this.selectedId) {
      this.renderDetail();
    }
    this.renderLegend();
    this.resize();
    if (this.nodes.length && (!this.fitted || this.nodes.length > this.fitCount * 1.8)) this.fit();
    const ranked = [...this.nodes].sort((a, b) =>
      ((b.degree || 0) + Math.sqrt(knownNumber(b.citationCount) ?? 0))
      - ((a.degree || 0) + Math.sqrt(knownNumber(a.citationCount) ?? 0)));
    ranked.slice(0, Math.min(12, this.nodes.length)).forEach(node => { node._label = true; });
    this.draw();
    this.settle();
  }

  destroy(): void {
    this.destroyed = true;
    if (typeof window.cancelAnimationFrame === 'function' && this.raf) {
      window.cancelAnimationFrame(this.raf);
    }
    this.observer?.disconnect();
    this.observer = null;
    this.host.innerHTML = '';
    this.canvas = null;
    this.ctx = null;
    this.detail = null;
    this.legend = null;
    this.empty = null;
  }
}

function byIdGet(map: Map<string, GraphNode>, id: unknown): GraphNode | undefined {
  return map.get(String(id));
}
