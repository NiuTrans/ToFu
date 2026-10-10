/* ═══════════════════════════════════════════════════════════════════
   settings/system-prompt-editor.ts — per-block system-prompt editor

   The General tab keeps a compact summary row + an "Edit…" button. The
   editor modal loads the built-in system prompt split into BLOCKS (one per
   section) and renders each with a keep/drop toggle, plus a free-text area
   for additional instructions appended after the built-in prompt.

   Source of truth (two hidden inputs on the General tab):
     #settingSystem               → config.systemPrompt (custom additions)
     #settingSystemDisabledBlocks → JSON array of disabled block IDs
                                     → config.systemPromptBlocks.disabled
   saveSettings() reads both; openSettings() seeds them from config. This
   module mirrors them into the modal and writes edits back on Apply.
   ═══════════════════════════════════════════════════════════════════ */

import { featureRegistry } from '../../feature-registry';

interface SystemPromptBlock {
  id: string;
  title?: string;
  text?: string;
  dynamic?: boolean;
}

interface SystemPromptBlocksResponse {
  blocks?: SystemPromptBlock[];
}

type SystemPromptEditorGlobals = Window & {
  Api?: {
    serverConfig?: {
      systemPromptBlocks(project: boolean, blocks: boolean): Promise<SystemPromptBlocksResponse | null>;
    };
  };
  config?: { projectPath?: unknown } | null;
  debugLog?: (message: string, level?: string) => void;
  t?: (key: string) => string;
  openSystemPromptEditor?: () => void;
  closeSystemPromptEditor?: () => void;
  applySystemPromptEditor?: () => void;
  resetSystemPromptBlocks?: () => void;
  _refreshSystemPromptSummary?: () => void;
};

function globals(): SystemPromptEditorGlobals {
  return featureRegistry as unknown as SystemPromptEditorGlobals;
}

function translate(key: string, fallback: string): string {
  const bridge = globals();
  return (typeof bridge.t === 'function') ? bridge.t(key) : fallback;
}

function inputValue(id: string): string {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement
    ? element.value
    : '';
}

/** Read the disabled-block-ID set from the hidden input (always an array). */
function getDisabledBlocks(): string[] {
  if (!document.getElementById('settingSystemDisabledBlocks')) return [];
  try {
    const arr: unknown = JSON.parse(inputValue('settingSystemDisabledBlocks') || '[]');
    return Array.isArray(arr) ? arr.filter((x) => !!x) : [];
  } catch {
    return [];
  }
}

/** Write the disabled-block-ID set back to the hidden input. */
function setDisabledBlocks(ids: string[]): void {
  const element = document.getElementById('settingSystemDisabledBlocks');
  if (element instanceof HTMLInputElement) {
    element.value = JSON.stringify(Array.from(new Set(ids || [])));
  }
}

/** Refresh the compact summary line on the General tab. */
function refreshSystemPromptSummary(): void {
  const summary = document.getElementById('settingSystemSummary');
  if (!summary) return;
  const val = inputValue('settingSystem').trim();
  const disabled = getDisabledBlocks();
  const parts: string[] = [];
  if (disabled.length) {
    const lbl = translate('settings.systemPromptBlocksOff', 'blocks off');
    parts.push(disabled.length + ' ' + lbl);
  }
  if (val) {
    const charsLbl = translate('settings.systemPromptSet', 'custom prompt set');
    parts.push(charsLbl + ' · ' + val.length + ' chars');
  }
  if (!parts.length) {
    summary.textContent = translate('settings.systemPromptEmpty', '(using all built-in blocks)');
    summary.classList.remove('has-value');
  } else {
    summary.textContent = parts.join(' · ');
    summary.classList.add('has-value');
  }
}

/* ── Modal preview mode ──
   Block visibility depends on project mode (code blocks) and tools. The
   modal previews tools-on by default with a checkbox to include the
   project/code blocks. The disabled SET is keyed on block ID and persists
   regardless of which preview mode is showing.

   Both mode variants are fetched ONCE on open and cached, so flipping the
   preview toggle re-renders instantly from memory — no network round-trip,
   no "Loading…" flash, no flicker. */
let sysPromptPreviewProject = false;
let sysPromptBlocksCache: { chat: SystemPromptBlock[] | null; project: SystemPromptBlock[] | null } = {
  chat: null,
  project: null,
};

/** IDs of blocks whose TEXT changes in project/code mode vs chat mode.
 *  These are the blocks the preview toggle actually affects, so we badge
 *  them so the user can see what "code/project mode" rewrites. */
function projectAffectedIds(): Record<string, boolean> {
  const chat = sysPromptBlocksCache.chat || [];
  const proj = sysPromptBlocksCache.project || [];
  const chatById: Record<string, string> = {};
  chat.forEach((b) => { chatById[b.id] = (b.text || ''); });
  const ids: Record<string, boolean> = {};
  proj.forEach((b) => {
    const ct = chatById[b.id];
    if (ct === undefined || ct !== (b.text || '')) ids[b.id] = true;
  });
  return ids;
}

/** Render the blocks list into the modal from a fetched blocks array. */
function renderSystemPromptBlocks(blocks: SystemPromptBlock[] | null): void {
  const list = document.getElementById('sysPromptBlocksList');
  if (!list) return;
  const disabled = getDisabledBlocks();
  const projectIds = projectAffectedIds();
  list.innerHTML = '';
  if (!blocks || !blocks.length) {
    list.innerHTML = '<div class="settings-toggle-desc">'
      + translate('settings.systemPromptLoadFailed', 'Failed to load built-in prompt') + '</div>';
    return;
  }
  const CHEVRON = '<svg class="sysprompt-block-chevron" viewBox="0 0 24 24" '
    + 'fill="none" stroke="currentColor" stroke-width="2.5" '
    + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<polyline points="9 18 15 12 9 6"></polyline></svg>';

  blocks.forEach((b) => {
    const off = disabled.indexOf(b.id) !== -1;
    const isProj = !!projectIds[b.id];
    const card = document.createElement('div');
    card.className = 'sysprompt-block' + (off ? ' is-off' : '')
      + (isProj ? ' is-project' : '');

    const header = document.createElement('div');
    header.className = 'sysprompt-block-head';
    header.setAttribute('role', 'button');
    header.setAttribute('tabindex', '0');

    const chev = document.createElement('span');
    chev.innerHTML = CHEVRON;

    const titleWrap = document.createElement('div');
    titleWrap.className = 'sysprompt-block-title';
    const titleText = document.createElement('span');
    titleText.textContent = b.title || b.id;
    titleWrap.appendChild(titleText);
    if (b.dynamic) {
      const badge = document.createElement('span');
      badge.className = 'sysprompt-block-badge';
      badge.textContent = translate('settings.systemPromptDynamic', 'dynamic');
      titleWrap.appendChild(badge);
    }
    if (isProj) {
      const pbadge = document.createElement('span');
      pbadge.className = 'sysprompt-block-badge is-project-badge';
      pbadge.textContent = translate('settings.systemPromptProjectBlock', 'project');
      pbadge.title = translate('settings.systemPromptProjectBlockTip', '');
      titleWrap.appendChild(pbadge);
    }

    const toggle = document.createElement('label');
    toggle.className = 'stg-toggle sysprompt-block-toggle';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !off;
    input.setAttribute('data-block-id', b.id);
    input.addEventListener('change', () => {
      const d = getDisabledBlocks();
      const idx = d.indexOf(b.id);
      if (input.checked) { if (idx !== -1) d.splice(idx, 1); }
      else if (idx === -1) { d.push(b.id); }
      setDisabledBlocks(d);
      card.classList.toggle('is-off', !input.checked);
    });
    const track = document.createElement('span');
    track.className = 'stg-toggle-track';
    track.innerHTML = '<span class="stg-toggle-thumb"></span>';
    toggle.appendChild(input);
    toggle.appendChild(track);
    // The toggle lives inside the click-to-expand header — stop its clicks
    // from also collapsing/expanding the card.
    toggle.addEventListener('click', (e) => { e.stopPropagation(); });

    if (chev.firstChild) header.appendChild(chev.firstChild);
    header.appendChild(titleWrap);
    header.appendChild(toggle);

    const pre = document.createElement('pre');
    pre.className = 'sysprompt-block-text';
    pre.textContent = b.text || '';

    const toggleOpen = () => { card.classList.toggle('is-open'); };
    header.addEventListener('click', toggleOpen);
    header.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleOpen(); }
    });

    card.appendChild(header);
    card.appendChild(pre);
    list.appendChild(card);
  });
}

/** (Re)render the preview-mode header (toggle + label) from current state. */
function renderModeHeader(): void {
  const modeEl = document.getElementById('sysPromptBlocksMode');
  if (!modeEl) return;
  const modeLabel = sysPromptPreviewProject
    ? translate('settings.systemPromptPreviewProject', 'preview: code/project mode')
    : translate('settings.systemPromptPreviewChat', 'preview: chat mode');
  modeEl.innerHTML = '<label class="sysprompt-preview-toggle">'
    + '<span class="stg-toggle stg-dv-toggle">'
    + '<input type="checkbox" id="sysPromptPreviewProjectCb"'
    + (sysPromptPreviewProject ? ' checked' : '') + '>'
    + '<span class="stg-toggle-track"><span class="stg-toggle-thumb"></span></span>'
    + '</span>'
    + '<span>' + translate('settings.systemPromptPreviewCode', 'show code/project blocks')
    + '</span></label> '
    + '<span class="sysprompt-blocks-mode-label">' + modeLabel + '</span>';
  const cb = document.getElementById('sysPromptPreviewProjectCb');
  // Flipping the preview is a PURE re-render from cache — no network, so the
  // list never collapses to a "Loading…" state and there is no flicker.
  if (cb) cb.addEventListener('change', () => {
    sysPromptPreviewProject = (cb as HTMLInputElement).checked;
    renderModeHeader();
    renderActiveBlocks();
  });
}

/** Render the cached blocks for the active preview mode (instant). */
function renderActiveBlocks(): void {
  const cached = sysPromptPreviewProject
    ? sysPromptBlocksCache.project : sysPromptBlocksCache.chat;
  renderSystemPromptBlocks(cached || []);
}

/** Fetch BOTH mode variants once, cache them, then render. */
async function loadSystemPromptBlocks(): Promise<void> {
  const list = document.getElementById('sysPromptBlocksList');
  // Only show the loading placeholder on a true cold load — never on a
  // preview toggle, which renders straight from cache.
  if (list && !sysPromptBlocksCache.chat && !sysPromptBlocksCache.project) {
    list.innerHTML = '<div class="settings-toggle-desc">'
      + translate('settings.systemPromptLoading', 'Loading built-in prompt…') + '</div>';
  }
  renderModeHeader();
  const bridge = globals();
  try {
    const results = await Promise.all([
      bridge.Api?.serverConfig?.systemPromptBlocks(false, true),
      bridge.Api?.serverConfig?.systemPromptBlocks(true, true),
    ]);
    sysPromptBlocksCache.chat = (results[0] && results[0].blocks) || [];
    sysPromptBlocksCache.project = (results[1] && results[1].blocks) || [];
    renderActiveBlocks();
  } catch (e) {
    if (typeof bridge.debugLog === 'function') {
      bridge.debugLog('[sysPromptEditor] loadBlocks failed: ' + ((e as Error)?.message), 'error');
    }
    renderSystemPromptBlocks([]);
  }
}

function openSystemPromptEditor(): void {
  const area = document.getElementById('sysPromptEditorArea');
  if (!document.getElementById('settingSystem') || !(area instanceof HTMLTextAreaElement)) return;
  area.value = inputValue('settingSystem');
  const status = document.getElementById('sysPromptEditorStatus');
  if (status) status.textContent = '';
  // Default the preview to the user's likely mode: if they have a project
  // path configured, show code blocks.
  try {
    sysPromptPreviewProject = !!globals().config?.projectPath;
  } catch {
    sysPromptPreviewProject = false;
  }
  document.getElementById('sysPromptModal')?.classList.add('open');
  sysPromptBlocksCache = { chat: null, project: null };
  void loadSystemPromptBlocks();
  setTimeout(() => { area.focus(); }, 50);
}

function closeSystemPromptEditor(): void {
  document.getElementById('sysPromptModal')?.classList.remove('open');
}

/** Write the editor content back to the hidden inputs (does NOT persist —
 *  saveSettings() does that when the user saves the settings panel). The
 *  disabled-block set is already kept in sync on every toggle change. */
function applySystemPromptEditor(): void {
  const src = document.getElementById('settingSystem');
  const area = document.getElementById('sysPromptEditorArea');
  if ((src instanceof HTMLTextAreaElement || src instanceof HTMLInputElement)
      && area instanceof HTMLTextAreaElement) {
    src.value = area.value;
  }
  refreshSystemPromptSummary();
  closeSystemPromptEditor();
}

/** Re-enable all built-in blocks (clear the disabled set). */
function resetSystemPromptBlocks(): void {
  setDisabledBlocks([]);
  // Clearing the disabled set is a pure re-render — reuse the cache if we
  // have it, otherwise cold-load.
  if (sysPromptBlocksCache.chat || sysPromptBlocksCache.project) {
    renderActiveBlocks();
  } else {
    void loadSystemPromptBlocks();
  }
}

const bridge = globals();
bridge.openSystemPromptEditor = openSystemPromptEditor;
bridge.closeSystemPromptEditor = closeSystemPromptEditor;
bridge.applySystemPromptEditor = applySystemPromptEditor;
bridge.resetSystemPromptBlocks = resetSystemPromptBlocks;
bridge._refreshSystemPromptSummary = refreshSystemPromptSummary;
