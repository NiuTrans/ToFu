/** Memory-modal presentation for the shared bounded skill-package installer. */
import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import {
  createSkillPackageInstaller,
  type SkillPackageInstallBody,
  type SkillPackageInstallResponse,
  type SkillPackageScope,
} from '../skills/package-installer';

type MemoryInstallBindings = Window & {
  Api?: {
    skills?: {
      install(form: FormData): Promise<SkillPackageInstallResponse | null>;
    };
  };
  t?: (key: I18nKey, values?: Record<string, unknown>) => string;
  debugLog?: (message: string, kind?: string) => void;
  _ephemeralToast?: (
    parent: Element,
    className: string,
    text: string,
    duration?: number,
  ) => Element | null;
  installSkillFromFileInput?: (input: HTMLInputElement | null) => void;
};

function bindings(): MemoryInstallBindings {
  return featureRegistry as unknown as MemoryInstallBindings;
}

function translate(
  key: I18nKey,
  values?: Record<string, unknown>,
): string {
  return bindings().t?.(key, values) || key;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || '');
}

function showMemoryInstallToast(message: string, isError = false): void {
  const modal = document.getElementById('memoryModal');
  const card = modal?.querySelector('.memory-modal');
  const toast = bindings()._ephemeralToast;
  if (card && toast) {
    toast(
      card,
      `memory-install-toast${isError ? ' is-error' : ''}`,
      message,
      isError ? 5_000 : 3_500,
    );
  } else {
    console.warn('[Memory] install toast port unavailable:', message);
  }
}

function selectedScope(): SkillPackageScope {
  const scope = document.querySelector<HTMLElement>('.memory-tab.active')
    ?.dataset.scope;
  return scope === 'global' ? 'global' : 'project';
}

function installedMessage(body: SkillPackageInstallBody): string {
  const name = String(body.memory?.name || '');
  const scope = String(body.memory?.scope || selectedScope());
  let message = translate('memory.installedPackage', { name, scope });
  if (body.replaced) message += translate('memory.replacedOld');
  const files = (body.install_hints ?? [])
    .map((hint) => String(hint.file || ''))
    .filter(Boolean);
  if (files.length) {
    message += translate('memory.installHintSuffix', { files: files.join(', ') });
  }
  return message;
}

const installer = createSkillPackageInstaller({
  install: (form) => {
    const api = bindings().Api?.skills;
    if (!api) throw new Error('Skills API is not ready');
    return api.install(form);
  },
  pickScope: () => selectedScope(),
  showInvalidFile: () => {
    showMemoryInstallToast(translate('memory.notZip'), true);
  },
  showInstalling: (file) => {
    showMemoryInstallToast(translate('memory.installingFile', {
      name: file.name,
    }));
  },
  showRejected: (detail) => {
    const message = translate('memory.installFailed', {
      err: errorMessage(detail) || translate('memory.noResponse'),
    });
    showMemoryInstallToast(message, true);
    bindings().debugLog?.(`Skill install failed: ${errorMessage(detail)}`, 'error');
  },
  showInstalled: (body) => {
    const message = installedMessage(body);
    showMemoryInstallToast(message);
    bindings().debugLog?.(message, 'success');
    bindings().debugLog?.(
      'Skill package installed — manage it in Settings → Skills tab',
    );
  },
  showError: (error) => {
    const detail = errorMessage(error);
    showMemoryInstallToast(translate('memory.installError', { err: detail }), true);
    bindings().debugLog?.(`Skill install error: ${detail}`, 'error');
  },
});

export function attachMemorySkillDropZone(): void {
  const modal = document.getElementById('memoryModal');
  const card = modal?.querySelector('.memory-modal');
  if (modal && card) installer.attachDropZone(modal, card);
}

export function installSkillFromFileInput(
  input: HTMLInputElement | null,
): void {
  installer.installFromInput(input);
}

bindings().installSkillFromFileInput = installSkillFromFileInput;
