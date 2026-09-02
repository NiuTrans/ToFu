/**
 * Research mode session owner — auto-research is a DIRECTION-scoped pipeline
 * (harvest → survey → idea gate → review) that needs no open paper, so it is
 * its own full-page mode, not a pane inside Paper mode.
 *
 * Mirrors paper/session.ts mechanics: paint chrome synchronously, hydrate
 * asynchronously, generation-guarded; mutually exclusive with Paper/ImageGen
 * mode (each session owner exits the others on enter).
 *
 * Owns: #researchModeContainer visibility, topbar button/title chrome, and
 * choosing between the landing (direction input + recent index) and the
 * console (_paintResearch) based on whether a stream exists.
 */
import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';

type ResearchSessionWindow = Window & Record<string, any>;

function globals(): ResearchSessionWindow {
  return featureRegistry as unknown as ResearchSessionWindow;
}

let researchMode = false;
let enterGeneration = 0;

const BACK_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>';
const RESEARCH_ICON = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2v7.527a2 2 0 0 1-.211.896L4.72 20.55a1 1 0 0 0 .9 1.45h12.76a1 1 0 0 0 .9-1.45l-5.069-10.127A2 2 0 0 1 14 9.527V2"/><path d="M8.5 2h7"/><path d="M7 16h10"/></svg>';

function translate(key: I18nKey, fallback: string): string {
  const value = globals().t?.(key);
  return typeof value === 'string' && value && value !== key ? value : fallback;
}

export function researchModeActive(): boolean {
  return researchMode;
}

function setResearchChrome(active: boolean): void {
  const container = document.getElementById('researchModeContainer');
  const chat = document.querySelector<HTMLElement>('.chat-wrapper');
  const input = document.querySelector<HTMLElement>('.input-area');
  if (container) container.style.display = active ? 'flex' : 'none';
  if (chat) chat.style.display = active ? 'none' : '';
  if (input) input.style.display = active ? 'none' : '';
  document.body?.classList.toggle('research-mode-active', active);

  const button = document.getElementById('researchModeBtn');
  if (button) {
    button.classList.toggle('active', active);
    button.innerHTML = active
      ? `${BACK_ICON}<span class="topbar-tool-label">${translate('topbar.backToChat', 'Back')}</span>`
      : `${RESEARCH_ICON}<span class="topbar-tool-label">${translate('topbar.research', 'Research')}</span>`;
    button.title = active
      ? translate('topbar.backToChat', 'Back to Chat')
      : translate('paper.research.entryTitle', 'Auto-research');
  }

  const topbar = document.getElementById('topbarTitle');
  if (topbar && active) {
    const label = translate('paper.research.entryTitle', 'Auto-research');
    topbar.textContent = label;
    topbar.title = label;
  }
}

function restoreChatTitle(): void {
  const state = globals();
  const topbar = document.getElementById('topbarTitle');
  if (!topbar) return;
  const conversations = Array.isArray(state.conversations) ? state.conversations : [];
  const conversation = state.activeConvId
    ? conversations.find((item: Record<string, any>) => item?.id === state.activeConvId)
    : null;
  const title = conversation?.title;
  topbar.textContent = !title || title === 'New Chat'
    ? translate('chat.newConversation', 'New Chat') : title;
  topbar.title = '';
}

/** Enter Research mode: show the console for a live stream, else the landing. */
export async function enterResearchMode(): Promise<void> {
  const state = globals();
  const generation = ++enterGeneration;
  if (state.imageGenMode) state.exitImageGenMode?.();
  if (state.paperMode) state.exitPaperMode?.();
  researchMode = true;

  // Before the first await: mode switches must never wait on a request.
  setResearchChrome(true);
  if (state._researchStream) state._paintResearch?.();
  else state._showResearchLanding?.();

  if (generation !== enterGeneration || !researchMode) return;
  state.debugLog?.('Research Mode: ENTER', 'success');
}

/** Tear down only client resources; the server-side job remains resumable. */
export function exitResearchMode(): void {
  if (!researchMode) return;
  enterGeneration += 1;
  researchMode = false;
  const state = globals();
  state._destroyResearchRuntime?.();
  try { restoreChatTitle(); } catch (error: unknown) {
    console.warn('[Research] restore topbar title failed:', error);
  }
  setResearchChrome(false);
  state._scheduleReflow?.();
  state.debugLog?.('Research Mode: EXIT', 'info');
}

export function toggleResearchMode(): void | Promise<void> {
  return researchMode ? exitResearchMode() : enterResearchMode();
}

export function installResearchSessionOwner(): void {
  const target = globals();
  target.enterResearchMode = enterResearchMode;
  target.exitResearchMode = exitResearchMode;
  target.toggleResearchMode = toggleResearchMode;
  target._researchModeActive = researchModeActive;
}

installResearchSessionOwner();
