/** Skills-panel composition for the shared bounded package installer. */
import { featureRegistry } from '../../feature-registry';
import { chooseSkillDestination } from './library';
import type { I18nKey } from '../../i18n';
import {
  createSkillPackageInstaller,
  type SkillPackageInstallBody,
  type SkillPackageInstallResponse,
} from './package-installer';

type SkillsInstallBindings = Window & {
  Api?: {
    skills?: {
      install(form: FormData, projectPath?: string): Promise<SkillPackageInstallResponse | null>;
    };
  };
  t?: (key: I18nKey, values?: Record<string, unknown>) => string;
  _ephemeralToast?: (
    parent: Element,
    className: string,
    text: string,
    duration?: number,
  ) => Element | null;
  _populateSkillsTab?: () => Promise<void>;
  _skillsInstallFromInput?: (input: HTMLInputElement | null) => void;
};

function bindings(): SkillsInstallBindings {
  return featureRegistry as unknown as SkillsInstallBindings;
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

export function showSkillsToast(
  message: string,
  kind?: 'error' | 'success',
): void {
  const className = `skills-toast${kind === 'error' ? ' is-error'
    : kind === 'success' ? ' is-success' : ''}`;
  const toast = bindings()._ephemeralToast;
  if (toast) {
    toast(document.body, className, message, kind === 'error' ? 5_000 : 3_500);
  } else {
    console.warn('[Skills] toast port unavailable:', message);
  }
}

function installedMessage(body: SkillPackageInstallBody): string {
  const name = String(body.memory?.name || '');
  let message = translate('skills.installedToast', { name });
  const files = (body.install_hints ?? [])
    .map((hint) => String(hint.file || ''))
    .filter(Boolean);
  if (files.length) {
    message += translate('skills.installHintSuffixUpload', {
      files: files.join(', '),
    });
  }
  return message;
}

let installProjectPath = '';
const installer = createSkillPackageInstaller({
  install: (form) => {
    const api = bindings().Api?.skills;
    if (!api) throw new Error('Skills API is not ready');
    return api.install(form, installProjectPath);
  },
  pickScope: async (file) => {
    const destination = await chooseSkillDestination(file.name);
    installProjectPath = destination?.projectPath || '';
    return destination?.scope || null;
  },
  showInvalidFile: () => showSkillsToast(translate('skills.notZip'), 'error'),
  showInstalling: (file) => {
    showSkillsToast(translate('skills.installingFile', { name: file.name }));
  },
  showRejected: (detail) => {
    showSkillsToast(translate('skills.installFailed', {
      err: errorMessage(detail) || translate('skills.noResponse'),
    }), 'error');
  },
  showInstalled: async (body) => {
    showSkillsToast(installedMessage(body), 'success');
    await bindings()._populateSkillsTab?.();
  },
  showError: (error) => {
    showSkillsToast(translate('skills.installError', {
      err: errorMessage(error),
    }), 'error');
  },
});

export function attachSkillsPackageDropZone(): void {
  const panel = document.getElementById('settingsTab_skills');
  const zone = document.getElementById('skillsDropZone');
  if (panel && zone) installer.attachDropZone(panel, zone);
}

export function installSkillsPackageFromInput(
  input: HTMLInputElement | null,
): void {
  installer.installFromInput(input);
}

bindings()._skillsInstallFromInput = installSkillsPackageFromInput;
