/*
 * Local deployment Settings projection.
 *
 * Responsibility: the dedicated 本地部署 page —
 *   1. 智能推荐: live hardware probe + curated catalog ranked against it
 *      (read-only /api/v4/local-serve/*), each card handing off to the chat
 *      agent for download + managed deploy;
 *   2. 云端替代: DeepSeek V4 Flash escape hatch when hardware is too weak,
 *      deep-linking the DeepSeek provider template wizard;
 *   3. 托管实例: ledger rows with live status; failed rows hand off to the
 *      agent's diagnosis/repair flow;
 *   4. 手动接入: engine preset endpoint probe-and-stage, managed model-path
 *      handoff, and the fully custom provider path.
 * Staging authority stays with the retained provider_render owner
 * (_stageModelRoutingProviderBundle); all deploy/stop/remove writes stay
 * with the approval-gated local_serve agent tools.
 */

import { featureRegistry } from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import { t } from '../../i18n';

interface LocalDeployPreset {
  engine: string;
  icon: string;
  name?: string;
  placeholder?: string;
  nameKey?: string;
  descKey: string;
  custom?: boolean;
}

interface LocalDeployGpu {
  index: number | string;
  name?: string;
  vram_free_bytes?: number;
  vram_total_bytes?: number;
}

interface LocalDeployHardware {
  gpus?: LocalDeployGpu[];
  ram_available_bytes?: number;
  ram_total_bytes?: number;
  disk_free_bytes?: number;
}

interface LocalDeployCandidate {
  id?: string;
  display?: string;
  tier?: string;
  params_b?: unknown;
  note?: string;
  best?: {
    variant?: string;
    engine?: string;
    weight_bytes?: number;
    download?: { hf_cmd?: string };
  };
}

interface LocalDeployCloudAlternative {
  display?: string;
  reason?: string;
  template_key?: string;
  pricing?: { input?: unknown; output?: unknown };
}

interface LocalDeployInstance {
  id?: string;
  served_name?: string;
  model_path?: string;
  status?: string;
  engine?: string;
  base_url?: string;
  diagnosis?: { summary?: string } | null;
}

interface LocalDeployProbeResult {
  provider_bundle?: Record<string, unknown> & { deployments?: unknown[] };
  error?: string;
  message?: string;
}

type LocalDeployGlobals = Window & {
  Api?: {
    localServe?: {
      hardware(): Promise<{ hardware?: LocalDeployHardware } | null>;
      recommendations(purpose: string): Promise<{
        candidates?: LocalDeployCandidate[];
        cloud_alternative?: LocalDeployCloudAlternative | null;
      } | null>;
      deployments(): Promise<{ instances?: LocalDeployInstance[] } | null>;
    };
    providers?: {
      probe(url: string, apiKey: string, path: string): Promise<LocalDeployProbeResult | null>;
    };
  };
  closeSettings?: () => void;
  newChat?: () => void;
  updateSendButton?: () => void;
  showAlert?: (message: string) => void;
  _brandSvg?: (brand: string, size: number) => string;
  _stageModelRoutingProviderBundle?: (bundle: unknown, apiKey: string) => Promise<boolean>;
  _addTemplateProvider?: (templateKey: string) => Promise<void>;
  addProvider?: () => void;
  addLocalProvider?: () => void;
};

function globals(): LocalDeployGlobals {
  return featureRegistry as unknown as LocalDeployGlobals;
}

const LOCAL_DEPLOY_PRESETS: LocalDeployPreset[] = [
  { engine: 'vllm', icon: 'vllm', name: 'vLLM',
    placeholder: 'http://10.0.0.5:8000/v1',
    descKey: 'settings.localPresetVllmDesc' },
  { engine: 'sglang', icon: 'sglang', name: 'SGLang',
    placeholder: 'http://10.0.0.5:30000/v1',
    descKey: 'settings.localPresetSglangDesc' },
  { engine: 'ollama', icon: 'ollama', name: 'Ollama',
    placeholder: 'http://localhost:11434/v1',
    descKey: 'settings.localPresetOllamaDesc' },
  { engine: 'llamacpp', icon: 'llamacpp', name: 'llama.cpp',
    placeholder: 'http://localhost:8080/v1',
    descKey: 'settings.localPresetLlamacppDesc' },
  { engine: 'managed', icon: 'local',
    nameKey: 'settings.localPresetManagedName',
    descKey: 'settings.localPresetManagedDesc' },
  // Custom comes LAST (owner-ratified 2026-07-25).
  { engine: '', icon: 'local', custom: true,
    nameKey: 'settings.localPresetCustomName',
    descKey: 'settings.localPresetCustomDesc' },
];

const LD_PURPOSES = ['', 'chat', 'coding', 'reasoning', 'vision', 'multilingual'];

const LD_TIER_CLASS: Record<string, string> = {
  comfortable: 'is-comfortable', tight: 'is-tight',
  extreme: 'is-extreme', cpu: 'is-cpu',
};

const LD_TIER_LABEL_KEY: Record<string, string> = {
  comfortable: 'settings.localRecFitComfortable',
  tight: 'settings.localRecFitTight',
  extreme: 'settings.localRecFitExtreme',
  cpu: 'settings.localRecFitCpu',
};

const LD_STATUS_LABEL_KEY: Record<string, string> = {
  planned: 'settings.localStatusPlanned',
  installing: 'settings.localStatusInstalling',
  starting: 'settings.localStatusStarting',
  running: 'settings.localStatusRunning',
  stopped: 'settings.localStatusStopped',
  failed: 'settings.localStatusFailed',
};

function brandSvg(brand: string, size: number): string {
  return globals()._brandSvg?.(brand, size) || '';
}

function translateKey(key: string): string {
  // Keys are assembled dynamically (purpose chips, tier/status label tables);
  // every produced key is a declared settings.* entry.
  return (t as (dynamicKey: string) => string)(key);
}

function localDeployWireClose(overlay: HTMLElement, modal: HTMLElement): () => void {
  function close() { overlay.remove(); }
  overlay.onclick = (event) => { if (event.target === overlay) close(); };
  const closeButton = modal.querySelector('.stg-modal-close');
  if (closeButton) (closeButton as HTMLElement).onclick = close;
  return close;
}

function localDeployPresetTile(preset: LocalDeployPreset, overlay: HTMLElement): HTMLButtonElement {
  const item = document.createElement('button');
  item.type = 'button';
  item.className = 'stg-template-item stg-local-preset';
  item.innerHTML = '<span class="stg-local-preset-icon">' +
    brandSvg(preset.icon, 22) + '</span>' +
    '<span class="stg-template-info"><span class="stg-template-name">' +
    escapeHtml(preset.nameKey ? translateKey(preset.nameKey) : String(preset.name)) +
    '</span><span class="stg-template-models">' +
    escapeHtml(translateKey(preset.descKey)) + '</span></span>' +
    '<span class="stg-local-preset-arrow"><svg viewBox="0 0 16 16" width="14" height="14" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M6 3.5 10.5 8 6 12.5"/></svg></span>';
  item.onclick = () => {
    overlay.remove();
    if (preset.engine === 'managed') openManagedDeployDialog();
    else if (preset.custom) globals().addProvider?.();
    else openLocalEndpointDialog(preset);
  };
  return item;
}

// ─────────────────────────── shared chat handoff ───────────────────────────

function ldStartChat(promptText: string): void {
  const bridge = globals();
  bridge.closeSettings?.();
  // newChat() reads the current draft to archive the previous conversation,
  // so the prompt must land in the input only after the new shell exists.
  bridge.newChat?.();
  const input = document.getElementById('userInput');
  if (!(input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement)) return;
  input.value = promptText;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  bridge.updateSendButton?.();
  input.focus();
}

function ldCopy(text: string, btn: HTMLElement): void {
  function done() {
    const prev = btn.textContent;
    btn.textContent = t('settings.localRecCopied');
    setTimeout(() => { btn.textContent = prev; }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    void navigator.clipboard.writeText(text).then(done, () => {});
  }
}

// ─────────────────────────── hardware card ───────────────────────────

function ldGib(n: unknown): string {
  return (typeof n === 'number' && isFinite(n)) ? (n / 1073741824).toFixed(1) : '?';
}

function ldHardwareHtml(hw: LocalDeployHardware | undefined): string {
  const parts: string[] = [];
  const gpus = (hw && hw.gpus) || [];
  if (gpus.length) {
    gpus.forEach((g) => {
      parts.push('<span class="stg-ld-hw-item">GPU' + escapeHtml(String(g.index)) + ' ' +
        escapeHtml(String(g.name || '?')) + ' · ' +
        escapeHtml(t('settings.localHwVram', {
          free: ldGib(g.vram_free_bytes), total: ldGib(g.vram_total_bytes),
        })) + '</span>');
    });
  } else {
    parts.push('<span class="stg-ld-hw-item stg-ld-hw-warn">' +
      escapeHtml(t('settings.localHwNoGpu')) + '</span>');
  }
  parts.push('<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localHwRam', {
      free: ldGib(hw && hw.ram_available_bytes),
      total: ldGib(hw && hw.ram_total_bytes),
    })) + '</span>');
  parts.push('<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localHwDisk', {
      free: ldGib(hw && hw.disk_free_bytes),
    })) + '</span>');
  return parts.join('');
}

function ldLoadHardware(modal: HTMLElement): void {
  const box = modal.querySelector('[data-ld="hardware"]');
  if (!box) return;
  box.innerHTML = '<span class="stg-ld-hw-item">' +
    escapeHtml(t('settings.localRecLoading')) + '</span>';
  void Promise.resolve(globals().Api?.localServe?.hardware()).then((data) => {
    if (!(box as HTMLElement).isConnected) return;
    if (!data || !data.hardware) {
      box.innerHTML = '<span class="stg-ld-hw-item stg-ld-hw-warn">' +
        escapeHtml(t('settings.localRecError')) + '</span>';
      return;
    }
    box.innerHTML = ldHardwareHtml(data.hardware);
  });
}

// ─────────────────────────── recommendations ───────────────────────────

function ldRecBrand(cand: LocalDeployCandidate): string {
  const id = String((cand && cand.id) || '');
  if (id.indexOf('qwen') === 0) return 'qwen';
  if (id.indexOf('deepseek') === 0) return 'deepseek';
  return 'local';
}

function ldRecCard(cand: LocalDeployCandidate): HTMLElement {
  const best = cand.best || {};
  const download = best.download || {};
  const tier = String(cand.tier || 'comfortable');
  const card = document.createElement('div');
  card.className = 'stg-ld-rec';
  card.innerHTML =
    '<span class="stg-ld-rec-icon">' + brandSvg(ldRecBrand(cand), 22) + '</span>' +
    '<span class="stg-ld-rec-main">' +
      '<span class="stg-ld-rec-title"><strong>' + escapeHtml(String(cand.display || '')) +
        '</strong><span class="stg-ld-fit ' + (LD_TIER_CLASS[tier] || '') + '">' +
        escapeHtml(translateKey(LD_TIER_LABEL_KEY[tier] || LD_TIER_LABEL_KEY.comfortable)) +
      '</span></span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(t('settings.localRecSub', {
          params: String(cand.params_b),
          variant: String(best.variant || ''),
          engine: String(best.engine || ''),
          size: ldGib(best.weight_bytes),
        })) + '</span>' +
      (cand.note ? '<span class="stg-ld-rec-note">' +
        escapeHtml(String(cand.note)) + '</span>' : '') +
      (download.hf_cmd ? '<span class="stg-ld-rec-cmd"><code>' +
        escapeHtml(String(download.hf_cmd)) +
        '</code><button type="button" class="stg-ld-copy">' +
        escapeHtml(t('settings.localRecCopyCmd')) + '</button></span>' : '') +
    '</span>' +
    '<button type="button" class="stg-btn-add stg-ld-rec-go">' +
      escapeHtml(t('settings.localRecDeploy')) + '</button>';
  const copyBtn = card.querySelector('.stg-ld-copy');
  if (copyBtn) {
    (copyBtn as HTMLElement).onclick = () => { ldCopy(String(download.hf_cmd || ''), copyBtn as HTMLElement); };
  }
  const goButton = card.querySelector('.stg-ld-rec-go');
  if (goButton) {
    (goButton as HTMLElement).onclick = () => {
      ldStartChat(t('settings.localRecDeployPrompt', {
        model: String(cand.display || ''),
        variant: String(best.variant || ''),
        engine: String(best.engine || ''),
        cmd: String(download.hf_cmd || ''),
      }));
    };
  }
  return card;
}

function ldCloudCard(alt: LocalDeployCloudAlternative): HTMLElement {
  const pricing = (alt && alt.pricing) || {};
  const card = document.createElement('div');
  card.className = 'stg-ld-cloud';
  card.innerHTML =
    '<span class="stg-ld-rec-icon">' + brandSvg('deepseek', 22) + '</span>' +
    '<span class="stg-ld-rec-main">' +
      '<span class="stg-ld-rec-title"><strong>' +
        escapeHtml(t('settings.localCloudTitle', { name: String(alt.display || '') })) +
      '</strong></span>' +
      '<span class="stg-ld-rec-note">' + escapeHtml(String(alt.reason || '')) + '</span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(t('settings.localCloudPrice', {
          input: String(pricing.input), output: String(pricing.output),
        })) + '</span>' +
    '</span>' +
    '<button type="button" class="stg-btn-add stg-ld-cloud-go">' +
      escapeHtml(t('settings.localCloudAdd')) + '</button>';
  const goButton = card.querySelector('.stg-ld-cloud-go');
  if (goButton) {
    (goButton as HTMLElement).onclick = () => {
      const overlay = document.getElementById('stgLocalDeployModal');
      if (overlay) overlay.remove();
      void globals()._addTemplateProvider?.(String(alt.template_key || 'deepseek'));
    };
  }
  return card;
}

function ldLoadRecommendations(modal: HTMLElement, purpose: string): void {
  const box = modal.querySelector('[data-ld="recs"]');
  if (!box) return;
  box.innerHTML = '<div class="stg-ld-hint">' +
    escapeHtml(t('settings.localRecLoading')) + '</div>';
  void Promise.resolve(globals().Api?.localServe?.recommendations(purpose)).then((data) => {
    if (!(box as HTMLElement).isConnected) return;
    box.innerHTML = '';
    if (!data) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localRecError')) + '</div>';
      return;
    }
    const candidates = data.candidates || [];
    if (!candidates.length) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localRecEmpty')) + '</div>';
    } else {
      candidates.forEach((cand) => { box.appendChild(ldRecCard(cand)); });
    }
    if (data.cloud_alternative) box.appendChild(ldCloudCard(data.cloud_alternative));
  });
}

function ldPurposeBar(modal: HTMLElement): HTMLElement {
  const bar = document.createElement('div');
  bar.className = 'stg-ld-purposes';
  LD_PURPOSES.forEach((purpose) => {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'stg-ld-purpose' + (purpose === '' ? ' is-active' : '');
    chip.textContent = translateKey('settings.localRecPurpose' +
      (purpose ? purpose.charAt(0).toUpperCase() + purpose.slice(1) : 'All'));
    chip.onclick = () => {
      bar.querySelectorAll('.stg-ld-purpose').forEach((row) => {
        row.classList.toggle('is-active', row === chip);
      });
      ldLoadRecommendations(modal, purpose);
    };
    bar.appendChild(chip);
  });
  return bar;
}

// ─────────────────────────── managed instances ───────────────────────────

function ldInstanceRow(inst: LocalDeployInstance): HTMLElement {
  const status = String(inst.status || '');
  const labelKey = LD_STATUS_LABEL_KEY[status];
  const row = document.createElement('div');
  row.className = 'stg-ld-inst';
  const diagnosis = inst.diagnosis || null;
  row.innerHTML =
    '<span class="stg-ld-inst-main">' +
      '<span class="stg-ld-rec-title"><strong>' +
        escapeHtml(String(inst.served_name || inst.id || '')) +
        '</strong><span class="stg-ld-state is-' + escapeHtml(status) + '">' +
        escapeHtml(labelKey ? translateKey(labelKey) : status) + '</span></span>' +
      '<span class="stg-ld-rec-sub">' +
        escapeHtml(String(inst.engine || '')) +
        (inst.base_url ? ' · ' + escapeHtml(String(inst.base_url)) : '') +
      '</span>' +
      (diagnosis ? '<span class="stg-ld-rec-note">' +
        escapeHtml(String(diagnosis.summary || '')) + '</span>' : '') +
    '</span>';
  if (status === 'failed') {
    const repair = document.createElement('button');
    repair.type = 'button';
    repair.className = 'stg-btn-add stg-ld-inst-fix';
    repair.textContent = t('settings.localManagedRepair');
    repair.onclick = () => {
      ldStartChat(t('settings.localRepairPrompt', {
        id: String(inst.id || ''),
        model: String(inst.served_name || inst.model_path || ''),
        engine: String(inst.engine || ''),
      }));
    };
    row.appendChild(repair);
  } else if (status === 'stopped' || status === 'planned') {
    const start = document.createElement('button');
    start.type = 'button';
    start.className = 'stg-btn-add stg-ld-inst-start';
    start.textContent = t('settings.localManagedStart');
    start.onclick = () => {
      ldStartChat(t('settings.localStartPrompt', {
        id: String(inst.id || ''),
        model: String(inst.served_name || inst.model_path || ''),
        engine: String(inst.engine || ''),
      }));
    };
    row.appendChild(start);
  }
  return row;
}

function ldLoadDeployments(modal: HTMLElement): void {
  const box = modal.querySelector('[data-ld="instances"]');
  if (!box) return;
  void Promise.resolve(globals().Api?.localServe?.deployments()).then((data) => {
    if (!(box as HTMLElement).isConnected) return;
    box.innerHTML = '';
    const rows = (data && data.instances) || [];
    if (!rows.length) {
      box.innerHTML = '<div class="stg-ld-hint">' +
        escapeHtml(t('settings.localManagedEmpty')) + '</div>';
      return;
    }
    rows.forEach((inst) => { box.appendChild(ldInstanceRow(inst)); });
  });
}

// ─────────────────────────── the page ───────────────────────────

function addLocalProvider(): void {
  const prev = document.getElementById('stgLocalDeployModal');
  if (prev) prev.remove();
  const overlay = document.createElement('div');
  overlay.id = 'stgLocalDeployModal';
  overlay.className = 'stg-modal-overlay';
  const modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard stg-local-deploy';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    escapeHtml(t('settings.localPresetTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.localPresetDesc')) + '</p>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localRecGroupTitle')) + '</div>' +
    '<div class="stg-ld-hw" data-ld="hardware"></div>' +
    '<div data-ld="purposes"></div>' +
    '<div class="stg-ld-recs" data-ld="recs"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localManagedGroup')) + '</div>' +
    '<div class="stg-ld-instances" data-ld="instances"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localPresetGroupEngines')) + '</div>' +
    '<div class="stg-template-grid" data-group="engines"></div>' +
    '<div class="stg-local-group-label">' +
    escapeHtml(t('settings.localPresetGroupOther')) + '</div>' +
    '<div class="stg-template-grid" data-group="other"></div></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  modal.querySelector('[data-ld="purposes"]')?.appendChild(ldPurposeBar(modal));
  const enginesGrid = modal.querySelector('[data-group="engines"]');
  const otherGrid = modal.querySelector('[data-group="other"]');
  LOCAL_DEPLOY_PRESETS.forEach((preset) => {
    const tile = localDeployPresetTile(preset, overlay);
    // Managed + custom stay below the four inference engines (owner-ratified
    // order 2026-07-25); grouping is purely visual.
    if (preset.engine === 'managed' || preset.custom) otherGrid?.appendChild(tile);
    else enginesGrid?.appendChild(tile);
  });
  localDeployWireClose(overlay, modal);
  ldLoadHardware(modal);
  ldLoadRecommendations(modal, '');
  ldLoadDeployments(modal);
}

function openLocalEndpointDialog(preset: LocalDeployPreset): void {
  const prev = document.getElementById('stgLocalEndpointModal');
  if (prev) prev.remove();
  const overlay = document.createElement('div');
  overlay.id = 'stgLocalEndpointModal';
  overlay.className = 'stg-modal-overlay';
  const modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    brandSvg(preset.icon, 18) + ' ' + escapeHtml(String(preset.name)) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(translateKey(preset.descKey)) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployEndpointsLabel')) +
    '<textarea class="stg-local-endpoints-input" rows="4" placeholder="' +
    escapeHtml(String(preset.placeholder)) + '"></textarea></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.localDeployApiKeyLabel')) +
    '<input type="password" class="stg-tpl-wizard-key" autocomplete="new-password"></label>' +
    '<div class="stg-auto-status" style="display:none"></div>' +
    '<div class="stg-local-endpoint-results"></div></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.localDeployProbeAdd')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  const close = localDeployWireClose(overlay, modal);
  const cancelButton = modal.querySelector('.stg-btn-secondary');
  if (cancelButton) (cancelButton as HTMLElement).onclick = close;
  const textarea = modal.querySelector('.stg-local-endpoints-input') as HTMLTextAreaElement;
  const keyInput = modal.querySelector('.stg-tpl-wizard-key') as HTMLInputElement;
  const statusEl = modal.querySelector('.stg-auto-status') as HTMLElement;
  const resultsEl = modal.querySelector('.stg-local-endpoint-results') as HTMLElement;
  const probeBtn = modal.querySelector('.stg-btn-add') as HTMLButtonElement;
  function setStatus(text: string, kind?: string) {
    statusEl.style.display = text ? '' : 'none';
    statusEl.className = 'stg-auto-status' + (kind ? ' ' + kind : '');
    statusEl.textContent = text;
  }
  probeBtn.onclick = async () => {
    const urls: string[] = [];
    textarea.value.split('\n').forEach((line) => {
      const url = line.trim();
      if (url && urls.indexOf(url) === -1) urls.push(url);
    });
    if (!urls.length) {
      globals().showAlert?.(t('settings.localDeployNoUrl'));
      return;
    }
    const apiKey = keyInput.value.trim();
    probeBtn.disabled = true;
    resultsEl.innerHTML = '';
    setStatus(t('settings.epProbingN', { n: urls.length }), 'stg-auto-loading');
    const bridge = globals();
    const probes = await Promise.allSettled(urls.map((url) => {
      return bridge.Api?.providers?.probe(url, apiKey, '');
    }));
    let okCount = 0;
    for (let i = 0; i < urls.length; i++) {
      const row = document.createElement('div');
      row.className = 'stg-local-endpoint-row';
      const probed = probes[i];
      const bundle = probed.status === 'fulfilled' &&
        probed.value && probed.value.provider_bundle;
      if (bundle) {
        try {
          // Sequential staging: each success bumps the v2 revision.
          if (await bridge._stageModelRoutingProviderBundle?.(bundle, apiKey)) {
            okCount++;
            row.classList.add('is-ok');
            row.textContent = urls[i] + ' · ' + t('settings.epModelsCount', {
              n: (bundle.deployments || []).length,
            });
          } else {
            row.classList.add('is-fail');
            row.textContent = urls[i] + ' · ' + t('settings.localDeployDuplicate');
          }
        } catch (error) {
          row.classList.add('is-fail');
          row.textContent = urls[i] + ' · ' + String((error as Error)?.message || error);
        }
      } else {
        const reason = probed.status === 'rejected'
          ? String((probed.reason as Error)?.message || probed.reason)
          : String((probed.status === 'fulfilled' && probed.value
              && (probed.value.error || probed.value.message)) ||
              t('settings.epProbeFailed'));
        row.classList.add('is-fail');
        row.textContent = urls[i] + ' · ' + reason;
      }
      resultsEl.appendChild(row);
    }
    if (okCount) {
      setStatus(t('settings.localDeployAddedSummary', {
        ok: okCount, total: urls.length,
      }), 'stg-auto-success');
    } else {
      setStatus(t('settings.localDeployNoneOk'), 'stg-auto-error');
    }
    probeBtn.disabled = false;
  };
  textarea.focus();
}

function openManagedDeployDialog(): void {
  const prev = document.getElementById('stgManagedDeployModal');
  if (prev) prev.remove();
  const overlay = document.createElement('div');
  overlay.id = 'stgManagedDeployModal';
  overlay.className = 'stg-modal-overlay';
  const modal = document.createElement('div');
  modal.className = 'stg-modal stg-tpl-wizard';
  const engineOptions = [
    { value: '', label: t('settings.managedDeployEngineAuto') },
    { value: 'vllm', label: 'vLLM' },
    { value: 'sglang', label: 'SGLang' },
    { value: 'ollama', label: 'Ollama' },
    { value: 'llamacpp', label: 'llama.cpp' },
  ];
  modal.innerHTML = '<div class="stg-modal-header"><span class="stg-modal-title">' +
    brandSvg('local', 18) + ' ' + escapeHtml(t('settings.managedDeployTitle')) +
    '</span><button type="button" class="stg-modal-close">✕</button></div>' +
    '<div class="stg-modal-body"><p class="stg-modal-desc">' +
    escapeHtml(t('settings.managedDeployDesc')) + '</p>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployPathLabel')) +
    '<input type="text" class="stg-tpl-wizard-key stg-managed-path" placeholder="' +
    escapeHtml(t('settings.managedDeployPathHint')) + '"></label>' +
    '<label class="stg-tpl-wizard-keylabel">' +
    escapeHtml(t('settings.managedDeployEngineLabel')) +
    '<select class="stg-tpl-wizard-key stg-managed-engine">' +
    engineOptions.map((option) => {
      return '<option value="' + option.value + '">' + escapeHtml(option.label) + '</option>';
    }).join('') + '</select></label></div>' +
    '<div class="stg-modal-footer"><button type="button" class="stg-btn-secondary">' +
    escapeHtml(t('settings.epBulkCancel')) + '</button>' +
    '<button type="button" class="stg-btn-add">' +
    escapeHtml(t('settings.managedDeployStart')) + '</button></div>';
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
  const close = localDeployWireClose(overlay, modal);
  const cancelButton = modal.querySelector('.stg-btn-secondary');
  if (cancelButton) (cancelButton as HTMLElement).onclick = close;
  const pathInput = modal.querySelector('.stg-managed-path') as HTMLInputElement;
  const engineSelect = modal.querySelector('.stg-managed-engine') as HTMLSelectElement;
  const startButton = modal.querySelector('.stg-btn-add');
  if (startButton) {
    (startButton as HTMLElement).onclick = () => {
      const path = pathInput.value.trim();
      if (!path) {
        globals().showAlert?.(t('settings.managedDeployPathRequired'));
        return;
      }
      const engineLabel = engineSelect.options[engineSelect.selectedIndex].text;
      close();
      startManagedDeployChat(path, engineLabel);
    };
  }
  pathInput.focus();
}

function startManagedDeployChat(path: string, engineLabel: string): void {
  const bridge = globals();
  bridge.closeSettings?.();
  // newChat() reads the current draft to archive the previous conversation,
  // so the prompt must land in the input only after the new shell exists.
  bridge.newChat?.();
  const input = document.getElementById('userInput');
  if (!(input instanceof HTMLTextAreaElement || input instanceof HTMLInputElement)) return;
  input.value = t('settings.managedDeployPrompt', { path: path, engine: engineLabel });
  input.dispatchEvent(new Event('input', { bubbles: true }));
  bridge.updateSendButton?.();
  input.focus();
}

const bridge = globals();
bridge.addLocalProvider = addLocalProvider;
