/**
 * Own one bounded skill-package upload and its page-lifetime ZIP drop zone.
 * Product-specific scope, messages, and post-install refresh stay in injected
 * presentation ports so Memory and Skills do not duplicate transport policy.
 */

export type SkillPackageScope = 'global' | 'project';

export interface SkillPackageInstallBody {
  readonly error?: unknown;
  readonly memory?: {
    readonly name?: unknown;
    readonly scope?: unknown;
  };
  readonly replaced?: unknown;
  readonly install_hints?: readonly {
    readonly file?: unknown;
  }[];
}

export interface SkillPackageInstallResponse {
  readonly ok: boolean;
  readonly statusText?: string;
  json(): Promise<unknown>;
}

export interface SkillPackageInstallerPorts {
  install(form: FormData): Promise<SkillPackageInstallResponse | null>;
  pickScope(file: File): Promise<SkillPackageScope | null> | SkillPackageScope | null;
  showInvalidFile(): void;
  showInstalling(file: File): void;
  showRejected(detail: unknown): void;
  showInstalled(body: SkillPackageInstallBody): void | PromiseLike<void>;
  showError(error: unknown): void;
}

export interface SkillPackageInstaller {
  attachDropZone(listenElement: Element, highlightElement: Element): void;
  install(file: File): Promise<void>;
  installFromInput(input: HTMLInputElement | null): void;
}

function isZip(file: File | null | undefined): file is File {
  return Boolean(file && (
    /\.zip$/i.test(file.name)
    || file.type === 'application/zip'
    || file.type === 'application/x-zip-compressed'
  ));
}

function carriesFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files');
}

async function responseBody(
  response: SkillPackageInstallResponse | null,
): Promise<SkillPackageInstallBody> {
  if (!response) return {};
  try {
    const body = await response.json();
    return body && typeof body === 'object'
      ? body as SkillPackageInstallBody : {};
  } catch {
    return {};
  }
}

export function createSkillPackageInstaller(
  ports: SkillPackageInstallerPorts,
): SkillPackageInstaller {
  let dropZoneAttached = false;
  let uploadActive = false;

  const install = async (file: File): Promise<void> => {
    if (!isZip(file)) {
      ports.showInvalidFile();
      return;
    }
    if (uploadActive) return;
    uploadActive = true;
    try {
      const scope = await ports.pickScope(file);
      if (scope === null) return;
      ports.showInstalling(file);
      const form = new FormData();
      form.append('file', file);
      form.append('scope', scope);
      const response = await ports.install(form);
      const body = await responseBody(response);
      if (!response?.ok) {
        ports.showRejected(body.error || response?.statusText);
        return;
      }
      await ports.showInstalled(body);
    } catch (error: unknown) {
      ports.showError(error);
    } finally {
      uploadActive = false;
    }
  };

  const installFromInput = (input: HTMLInputElement | null): void => {
    const file = input?.files?.[0];
    if (!file) return;
    void install(file);
    input.value = '';
  };

  const attachDropZone = (
    listenElement: Element,
    highlightElement: Element,
  ): void => {
    if (dropZoneAttached) return;
    dropZoneAttached = true;
    let dragDepth = 0;
    listenElement.addEventListener('dragenter', (event) => {
      const dragEvent = event as DragEvent;
      if (!carriesFiles(dragEvent)) return;
      dragEvent.preventDefault();
      dragDepth += 1;
      highlightElement.classList.add('is-dragging');
    });
    listenElement.addEventListener('dragover', (event) => {
      const dragEvent = event as DragEvent;
      if (!carriesFiles(dragEvent)) return;
      dragEvent.preventDefault();
      if (dragEvent.dataTransfer) dragEvent.dataTransfer.dropEffect = 'copy';
    });
    listenElement.addEventListener('dragleave', (event) => {
      const dragEvent = event as DragEvent;
      if (!carriesFiles(dragEvent)) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) highlightElement.classList.remove('is-dragging');
    });
    listenElement.addEventListener('drop', (event) => {
      const dragEvent = event as DragEvent;
      if (!carriesFiles(dragEvent)) return;
      dragEvent.preventDefault();
      dragDepth = 0;
      highlightElement.classList.remove('is-dragging');
      const files = Array.from(dragEvent.dataTransfer?.files ?? []);
      const file = files.find(isZip);
      if (file) void install(file);
      else if (files.length) ports.showInvalidFile();
    });
  };

  return Object.freeze({ attachDropZone, install, installFromInput });
}
