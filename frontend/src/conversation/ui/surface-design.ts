/** Default conversation appearance: CSS, shell classes, controls, copy and
 * renderer composition. No synchronization, scheduling or window policy. */
import './conversation-surface.css';
import { createButton } from '../../ui';
import { t } from '../../i18n';
import { markdownImagePreviewSource } from '../../markdown/images';
import type { ToolPanelEntry } from '../presentation/grouped-turn-layout';
import { RoundOutline } from './round-outline';
import { disclosureChevron } from './renderers/icons';
import type { ConversationSurfaceOptions } from './surface-contracts';
import type { ConversationSurfaceDesign, SurfacePart, SurfaceTransportNotice } from './surface-design-contracts';
import {
  renderDefaultBlock, renderDefaultTurnAvatar, renderDefaultTurnHeader,
  renderDefaultTurnActions, renderDefaultTurnFooter, renderDefaultTurnContextRail,
  renderDefaultPlanDecision, renderDefaultLaneHeader, renderDefaultQueueItem,
} from './surface-default-renderers';

const PART_CLASSES: Record<SurfacePart, string> = {
  root: 'conversation-surface',
  'main-lane': 'conversation-lane conversation-lane--main',
  'branch-lane': 'conversation-lane conversation-lane--branch branch-panel',
  'branch-turns': 'conversation-lane-turns branch-messages',
  'orphan-lane': 'conversation-lane conversation-lane--orphan',
  turn: 'conversation-turn', block: 'conversation-block',
  'queue-item': 'conversation-queue-item',
  'turn-content': 'conversation-turn-content message-content',
  'turn-blocks': 'conversation-turn-blocks message-body',
  'context-rail': 'conversation-turn-context-rail turn-ctx',
  'context-card': 'conversation-turn-context-card tctx-card',
  'context-content': 'turn-context-content',
};

const TRANSPORT_LABELS: Record<SurfaceTransportNotice, string> = {
  recovering: 'Reconnecting — the transcript catches up automatically; '
    + 'in-flight replies are not lost.',
  degraded: 'Connection degraded — the transcript below may be stale while it resyncs.',
  offline: 'You appear to be offline — the transcript resyncs when the connection returns.',
};

// Compare actual DOM, not a second presentation cache: external repairs and
// renderer-owned mutations remain visible on the next commit.
function className(node: HTMLElement, value: string): void {
  if (node.className !== value) node.className = value;
}

function text(node: HTMLElement, value: string): void {
  if (node.textContent !== value) node.textContent = value;
}

/* One retained child per design slot, so keyboard focus survives repaint. */
function slot<T extends HTMLElement>(parent: HTMLElement, name: string, create: () => T): T {
  return parent.querySelector<T>(`:scope > .${name}`) ?? parent.appendChild(create());
}

function span(document: Document, name: string): HTMLSpanElement {
  const node = document.createElement('span');
  node.className = name;
  return node;
}

const plural = (count: number): string => (count === 1 ? '' : 's');

const SVG_NS = 'http://www.w3.org/2000/svg';
const WRENCH = ['M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94'
  + 'l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z'];
const NOTES = ['M4 6h16', 'M4 12h16', 'M4 18h10'];
const CHEVRONS_UP = ['m17 11-5-5-5 5', 'm17 18-5-5-5 5'];
const CHEVRONS_DOWN = ['m7 6 5 5 5-5', 'm7 13 5 5 5-5'];

/* Decorative line glyph in the tool rows' own icon grammar (24px grid). */
function glyph(document: Document, name: string, paths: readonly string[]): HTMLSpanElement {
  const wrapper = span(document, name);
  wrapper.setAttribute('aria-hidden', 'true');
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', d);
    svg.appendChild(path);
  }
  wrapper.appendChild(svg);
  return wrapper;
}

const TOOL_PANEL_BOUNDARY_KEYS = { earlier: 'toolPanel.hidden', later: 'toolPanel.hiddenLater' } as const;

/* "N tools used · M turns" settled, "Working… (N)" while a call runs. Rounds
 * show only when they group calls; one call per round adds no information. */
function toolPanelLabel(panel: ToolPanelEntry): string {
  if (panel.active) return t('toolPanel.working', { n: panel.toolCount });
  const rounds = panel.roundCount > 0 && panel.roundCount < panel.toolCount
    ? t('toolPanel.turnsSuffix', { n: panel.roundCount, s: plural(panel.roundCount) }) : '';
  return t('toolPanel.toolsUsed', { n: panel.toolCount, s: plural(panel.toolCount) }) + rounds;
}

function boundaryButton(control: HTMLElement): HTMLButtonElement {
  if (!control.classList.contains('conversation-history-boundary')) {
    control.classList.add('conversation-history-boundary');
  }
  let button = control.querySelector<HTMLButtonElement>('button');
  if (!button) {
    button = createButton({ label: '', variant: 'ghost', size: 'sm' }, control.ownerDocument);
    button.classList.add('conversation-history-fallback');
    control.append(button);
  }
  return button;
}

export function createConversationSurfaceDesign(options: ConversationSurfaceOptions): ConversationSurfaceDesign {
  return {
    createRoundOutline: (container, revealBlock) => new RoundOutline(container, options.roundOutlineLabel, revealBlock),
    // Read custom ports at use time: renderer replacement must retain its
    // existing identity invalidation, including after a failed commit.
    renderers: {
      get renderBlock() { return options.renderBlock ?? renderDefaultBlock; },
      get renderTurnAvatar() { return options.renderTurnAvatar ?? renderDefaultTurnAvatar; },
      get renderTurnHeader() { return options.renderTurnHeader ?? renderDefaultTurnHeader; },
      get renderTurnActions() { return options.renderTurnActions ?? renderDefaultTurnActions; },
      get renderTurnFooter() { return options.renderTurnFooter ?? renderDefaultTurnFooter; },
      get renderTurnContextRail() { return options.renderTurnContextRail ?? renderDefaultTurnContextRail; },
      get renderPlanDecision() { return options.renderPlanDecision ?? renderDefaultPlanDecision; },
      get renderLaneHeader() { return options.renderLaneHeader ?? renderDefaultLaneHeader; },
      get renderQueueItem() { return options.renderQueueItem ?? renderDefaultQueueItem; },
    },
    decorate(node, part) { className(node, PART_CLASSES[part]); },
    turnShell(node, turn) {
      className(node, `conversation-turn message${turn.actor === 'human' ? ' user-msg' : ''}${
        turn.actor !== 'human' && (turn.status === 'failed' || turn.status === 'interrupted')
          ? ' turn-failed' : ''}`);
    },
    toolPanelHeader(node, panel) {
      // The panel is its own paper card, so its header leads with a glyph tile
      // in the rows' icon grammar instead of sharing the prose text edge.
      className(node, 'conversation-block conversation-tool-panel');
      const document = node.ownerDocument;
      const button = slot(node, 'conversation-tool-panel__toggle', () => {
        const created = document.createElement('button');
        created.type = 'button';
        created.className = 'conversation-tool-panel__toggle';
        return created;
      });
      const tile = slot(button, 'conversation-tool-panel__glyph',
        () => glyph(document, 'conversation-tool-panel__glyph', WRENCH));
      if (button.firstElementChild !== tile) button.prepend(tile);
      const label = slot(button, 'conversation-tool-panel__label',
        () => span(document, 'conversation-tool-panel__label'));
      text(label, toolPanelLabel(panel));
      if (panel.active) label.setAttribute('role', 'status');
      else label.removeAttribute('role');
      const errors = slot(button, 'conversation-tool-panel__errors',
        () => span(document, 'conversation-tool-panel__errors'));
      errors.hidden = panel.errorCount === 0;
      text(errors, panel.errorCount
        ? t('toolPanel.errorCount', { n: panel.errorCount, s: plural(panel.errorCount) }) : '');
      const chevron = slot(button, 'conversation-tool-panel__chevron',
        () => disclosureChevron(document, 'conversation-tool-panel__chevron'));
      if (button.lastElementChild !== chevron) button.appendChild(chevron);
      return button;
    },
    narrationWell(document) {
      const well = document.createElement('div');
      well.className = 'conversation-narration';
      well.dataset.narrationWell = '';
      const body = document.createElement('div');
      body.className = 'conversation-narration__body';
      body.dataset.narrationWellBody = '';
      // A bounded scroller must be reachable by keyboard to be scrollable.
      body.tabIndex = 0;
      body.setAttribute('role', 'region');
      well.append(body);
      return well;
    },
    paintNarrationWell(well, count) {
      // The activity card's last fold: its header row repeats the tool
      // panel's toggle grammar (glyph tile, label, trailing chevron).
      const document = well.ownerDocument;
      const toggle = slot(well, 'conversation-narration__toggle', () => {
        const created = document.createElement('button');
        created.type = 'button';
        created.className = 'conversation-narration__toggle';
        return created;
      });
      if (well.firstElementChild !== toggle) well.prepend(toggle);
      const tile = slot(toggle, 'conversation-narration__glyph',
        () => glyph(document, 'conversation-narration__glyph', NOTES));
      if (toggle.firstElementChild !== tile) toggle.prepend(tile);
      const value = t('narrationWell.label', { n: count, s: plural(count) });
      text(slot(toggle, 'conversation-narration__label',
        () => span(document, 'conversation-narration__label')), value);
      const chevron = slot(toggle, 'conversation-narration__chevron',
        () => disclosureChevron(document, 'conversation-narration__chevron'));
      if (toggle.lastElementChild !== chevron) toggle.appendChild(chevron);
      const body = well.querySelector<HTMLElement>(':scope > [data-narration-well-body]');
      if (body && body.getAttribute('aria-label') !== value) body.setAttribute('aria-label', value);
      return toggle;
    },
    transportNotice(node, state) {
      className(node, `conversation-surface__transport-notice conversation-surface__transport-notice--${state}`);
      node.setAttribute('role', 'status');
      text(node, options.transportNoticeLabel?.(state) ?? TRANSPORT_LABELS[state]);
    },
    toolPanelBoundary(node, direction, count) {
      className(node, 'conversation-tool-panel-boundary');
      const document = node.ownerDocument;
      let button = node.querySelector<HTMLButtonElement>(':scope > button');
      if (!button) {
        button = document.createElement('button');
        button.type = 'button';
        button.className = 'conversation-tool-panel-boundary__button';
        node.appendChild(button);
      }
      let mark = button.querySelector<HTMLElement>(':scope > .conversation-tool-panel-boundary__glyph');
      if (mark?.dataset.direction !== direction) {
        const next = glyph(document, 'conversation-tool-panel-boundary__glyph',
          direction === 'earlier' ? CHEVRONS_UP : CHEVRONS_DOWN);
        next.dataset.direction = direction;
        if (mark) mark.replaceWith(next); else button.prepend(next);
      }
      const label = slot(button, 'conversation-tool-panel-boundary__label',
        () => span(document, 'conversation-tool-panel-boundary__label'));
      text(label, t(TOOL_PANEL_BOUNDARY_KEYS[direction], { n: count }));
      return button;
    },
    blockBoundary(node, direction, count) {
      const button = boundaryButton(node);
      text(button, direction === 'earlier'
        ? options.windowing?.blockLabel?.(count) ?? `View earlier content (${count} items)`
        : options.windowing?.label?.('later', count) ?? `View later content (${count} items)`);
      return button;
    },
    windowBoundary(node, direction, count, state) {
      let status = node.querySelector<HTMLElement>('[role="status"]');
      if (!status) {
        status = node.ownerDocument.createElement('span');
        status.setAttribute('role', 'status'); node.append(status);
      }
      status.hidden = !state;
      text(status, state ? options.windowing?.historyStatusLabel?.(state)
        ?? (state === 'loading' ? 'Loading earlier messages…'
          : 'Could not load earlier messages. Scroll up or retry.') : '');
      const button = boundaryButton(node);
      text(button, state === 'failed' ? options.windowing?.historyRetryLabel?.() ?? 'Retry'
        : options.windowing?.label?.(direction, count) ?? String(count));
      return button;
    },
    imagePreview(event, target, open) {
      const markdownImage = target.closest<HTMLImageElement>('.md-content img.md-image');
      if (markdownImage) {
        if (markdownImage.classList.contains('md-preview-image')
            && !markdownImage.closest('a, button') && open) {
          const source = markdownImagePreviewSource(markdownImage);
          if (source) { event.preventDefault(); open(source); }
        }
        return true;
      }
      const preview = target.closest<HTMLElement>('[data-preview-src]');
      if (!preview) return false;
      const source = (preview.dataset.previewSrc || '').trim();
      if (/^(?:https?:|blob:)/i.test(source)
          || /^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(source)
          || /^(?:\/|\.\.?\/)/.test(source)) {
        event.preventDefault(); open?.(source);
      }
      return true;
    },
    isImagePreviewTarget: target => target.matches('.md-content img.md-preview-image'),
  };
}
