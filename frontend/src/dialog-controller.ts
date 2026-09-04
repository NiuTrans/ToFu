/**
 * Responsibility: own the single active application dialog, its DOM, focus,
 * keyboard navigation, Promise settlement, animation, and live-check timers.
 * Entry point: createAppDialogController. Dependencies: injected document,
 * scheduler, localized copy, and warning logger only.
 */

export interface AppDialogSchedule {
  setTimeout(callback: () => void, delayMs: number): number;
  clearTimeout(handle: number): void;
  setInterval(callback: () => void, delayMs: number): number;
  clearInterval(handle: number): void;
  requestAnimationFrame(callback: FrameRequestCallback): number;
  cancelAnimationFrame(handle: number): void;
}

export interface AppDialogCopy {
  confirm(): string;
  cancel(): string;
  ok(): string;
}

export interface AppDialogLogger {
  warn(message: string, detail?: unknown): void;
}

export interface ConfirmDialogOptions {
  readonly title?: string;
  readonly okText?: string;
  readonly cancelText?: string;
  readonly danger?: boolean;
}

export interface AlertDialogOptions {
  readonly title?: string;
  readonly okText?: string;
}

export interface PromptDialogOptions extends ConfirmDialogOptions {
  readonly defaultValue?: string;
  readonly placeholder?: string;
}

export interface ChoiceDialogOption<Value extends string = string> {
  readonly value: Value;
  readonly label: string;
  readonly subtitle?: string;
  /** Trusted application-authored inline SVG only; never user/model content. */
  readonly icon?: string;
  readonly accent?: boolean;
}

export interface ChoiceDialogOptions<Value extends string = string> {
  readonly title?: string;
  readonly message?: string;
  readonly options?: readonly ChoiceDialogOption<Value>[];
  readonly dismissValue?: Value;
  readonly liveCheck?: () => boolean;
}

export interface AppDialogController {
  showConfirm(
    message: unknown,
    options?: ConfirmDialogOptions,
  ): Promise<boolean>;
  showAlert(
    message: unknown,
    options?: AlertDialogOptions,
  ): Promise<boolean>;
  showPrompt(
    message: unknown,
    options?: PromptDialogOptions,
  ): Promise<string | null>;
  showChoice<Value extends string = string>(
    options?: ChoiceDialogOptions<Value>,
  ): Promise<Value | null>;
  destroy(): void;
}

export interface AppDialogControllerPorts {
  readonly document: Document;
  readonly schedule: AppDialogSchedule;
  readonly copy: AppDialogCopy;
  readonly log: AppDialogLogger;
}

interface ActiveDialogSession {
  closeWithDefault(immediate: boolean): void;
}

const COPY_KEYS = Object.freeze({
  confirm: 'dialog.confirm',
  cancel: 'dialog.cancel',
  ok: 'dialog.ok',
});

const FALLBACK_COPY = Object.freeze({
  confirm: '确定',
  cancel: '取消',
  ok: '好的',
});

const EXIT_ANIMATION_MS = 160;
const LIVE_CHECK_INTERVAL_MS = 250;

export function createAppDialogController(
  ports: AppDialogControllerPorts,
): AppDialogController {
  const browserDocument = ports.document;
  let activeSession: ActiveDialogSession | null = null;
  let destroyed = false;

  const copyOrFallback = (
    read: () => string,
    key: string,
    fallback: string,
  ): string => {
    try {
      const value = read();
      if (value && value !== key) return value;
    } catch (error: unknown) {
      ports.log.warn('[AppDialog] copy lookup failed', error);
    }
    return fallback;
  };

  const appendMessage = (element: HTMLElement, message: unknown): void => {
    const lines = String(message ?? '').split('\n');
    lines.forEach((line, index) => {
      if (index > 0) element.appendChild(browserDocument.createElement('br'));
      element.appendChild(browserDocument.createTextNode(line));
    });
  };

  const replaceActiveDialog = (): void => {
    activeSession?.closeWithDefault(true);
    activeSession = null;
  };

  const createCard = (
    className: string,
    role: 'dialog' | 'alertdialog',
    title?: string,
    message?: unknown,
    includeEmptyMessage = false,
  ): { overlay: HTMLDivElement; card: HTMLDivElement } => {
    const overlay = browserDocument.createElement('div');
    overlay.className = 'app-dialog-overlay';
    const card = browserDocument.createElement('div');
    card.className = className;
    card.setAttribute('role', role);
    card.setAttribute('aria-modal', 'true');
    if (title) {
      const titleElement = browserDocument.createElement('div');
      titleElement.className = 'app-dialog-title';
      titleElement.textContent = title;
      card.appendChild(titleElement);
    }
    if (includeEmptyMessage
        || (message !== undefined && message !== null && String(message) !== '')) {
      const body = browserDocument.createElement('div');
      body.className = 'app-dialog-message';
      appendMessage(body, message);
      card.appendChild(body);
    }
    overlay.appendChild(card);
    browserDocument.body.appendChild(overlay);
    return { overlay, card };
  };

  const createSession = <Result>(
    overlay: HTMLDivElement,
    defaultResult: Result,
  ): {
    promise: Promise<Result>;
    close(result: Result, immediate?: boolean): void;
    listenForKeys(listener: (event: KeyboardEvent) => void): void;
    startAnimation(callback: () => void): void;
    startLiveCheck(check: () => boolean): void;
  } => {
    let settlePromise: (result: Result) => void = () => undefined;
    const promise = new Promise<Result>((resolve) => { settlePromise = resolve; });
    const previousFocus = browserDocument.activeElement;
    let settled = false;
    let keyListener: ((event: KeyboardEvent) => void) | null = null;
    let liveTimer: number | null = null;
    let animationFrame: number | null = null;
    let removalTimer: number | null = null;
    let session: ActiveDialogSession;

    const clearInteractiveResources = (): void => {
      if (keyListener) {
        browserDocument.removeEventListener('keydown', keyListener, true);
        keyListener = null;
      }
      if (liveTimer !== null) {
        ports.schedule.clearInterval(liveTimer);
        liveTimer = null;
      }
      if (animationFrame !== null) {
        ports.schedule.cancelAnimationFrame(animationFrame);
        animationFrame = null;
      }
    };

    const removeOverlay = (): void => {
      if (removalTimer !== null) {
        ports.schedule.clearTimeout(removalTimer);
        removalTimer = null;
      }
      overlay.remove();
      if (activeSession === session) activeSession = null;
    };

    const restorePreviousFocus = (): void => {
      try {
        const focus = (previousFocus as HTMLElement | null)?.focus;
        if (typeof focus === 'function') focus.call(previousFocus);
      } catch (error: unknown) {
        ports.log.warn('[AppDialog] previous focus restore failed', error);
      }
    };

    const close = (result: Result, immediate = false): void => {
      if (settled) {
        if (immediate) removeOverlay();
        return;
      }
      settled = true;
      clearInteractiveResources();
      overlay.onclick = null;
      if (immediate) {
        removeOverlay();
      } else {
        overlay.classList.add('closing');
        removalTimer = ports.schedule.setTimeout(
          removeOverlay,
          EXIT_ANIMATION_MS,
        );
      }
      restorePreviousFocus();
      settlePromise(result);
    };

    session = {
      closeWithDefault(immediate: boolean): void {
        close(defaultResult, immediate);
      },
    };
    activeSession = session;

    return {
      promise,
      close,
      listenForKeys(listener): void {
        keyListener = listener;
        browserDocument.addEventListener('keydown', listener, true);
      },
      startAnimation(callback): void {
        animationFrame = ports.schedule.requestAnimationFrame(() => {
          animationFrame = null;
          if (!settled) callback();
        });
      },
      startLiveCheck(check): void {
        liveTimer = ports.schedule.setInterval(() => {
          let live = true;
          try {
            live = check() === true;
          } catch (error: unknown) {
            ports.log.warn('[AppDialog] live check failed', error);
          }
          if (!live) close(defaultResult);
        }, LIVE_CHECK_INTERVAL_MS);
      },
    };
  };

  const createActionButton = (
    className: string,
    label: string,
  ): HTMLButtonElement => {
    const button = browserDocument.createElement('button');
    button.type = 'button';
    button.className = className;
    button.textContent = label;
    return button;
  };

  const showBasicDialog = <Result>(config: {
    readonly message: unknown;
    readonly title?: string;
    readonly okText: string;
    readonly cancelText: string | null;
    readonly danger?: boolean;
    readonly prompt?: boolean;
    readonly defaultValue?: string;
    readonly placeholder?: string;
    readonly cancelResult: Result;
    readonly okResult: (input: HTMLInputElement | null) => Result;
  }): Promise<Result> => {
    if (destroyed) return Promise.resolve(config.cancelResult);
    replaceActiveDialog();
    const { overlay, card } = createCard(
      `app-dialog${config.danger ? ' is-danger' : ''}`,
      config.prompt ? 'dialog' : 'alertdialog',
      config.title,
      config.message,
      true,
    );
    let input: HTMLInputElement | null = null;
    if (config.prompt) {
      input = browserDocument.createElement('input');
      input.type = 'text';
      input.className = 'app-dialog-input';
      input.value = String(config.defaultValue ?? '');
      if (config.placeholder) input.placeholder = config.placeholder;
      card.appendChild(input);
    }
    const actions = browserDocument.createElement('div');
    actions.className = 'app-dialog-actions';
    let cancelButton: HTMLButtonElement | null = null;
    if (config.cancelText !== null) {
      cancelButton = createActionButton(
        'app-dialog-btn app-dialog-cancel',
        config.cancelText,
      );
      actions.appendChild(cancelButton);
    }
    const okButton = createActionButton(
      `app-dialog-btn app-dialog-ok${config.danger ? ' is-danger' : ''}`,
      config.okText,
    );
    actions.appendChild(okButton);
    card.appendChild(actions);

    const session = createSession(overlay, config.cancelResult);
    okButton.addEventListener(
      'click',
      () => session.close(config.okResult(input)),
    );
    cancelButton?.addEventListener(
      'click',
      () => session.close(config.cancelResult),
    );
    overlay.onclick = (event) => {
      if (event.target === overlay) session.close(config.cancelResult);
    };
    session.listenForKeys((event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        session.close(config.cancelResult);
        return;
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        if (config.prompt) {
          session.close(config.okResult(input));
        } else if (cancelButton
            && browserDocument.activeElement === cancelButton) {
          session.close(config.cancelResult);
        } else {
          session.close(config.okResult(input));
        }
        return;
      }
      if (!config.prompt && cancelButton
          && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        (event.key === 'ArrowLeft' ? cancelButton : okButton).focus();
      }
    });
    session.startAnimation(() => {
      overlay.classList.add('open');
      if (input) {
        input.focus();
        input.select();
      } else {
        okButton.focus();
      }
    });
    return session.promise;
  };

  const showConfirm = (
    message: unknown,
    options: ConfirmDialogOptions = {},
  ): Promise<boolean> => showBasicDialog({
    message,
    title: options.title,
    okText: options.okText || copyOrFallback(
      ports.copy.confirm,
      COPY_KEYS.confirm,
      FALLBACK_COPY.confirm,
    ),
    cancelText: options.cancelText || copyOrFallback(
      ports.copy.cancel,
      COPY_KEYS.cancel,
      FALLBACK_COPY.cancel,
    ),
    danger: options.danger === true,
    cancelResult: false,
    okResult: () => true,
  });

  const showAlert = (
    message: unknown,
    options: AlertDialogOptions = {},
  ): Promise<boolean> => showBasicDialog({
    message,
    title: options.title,
    okText: options.okText || copyOrFallback(
      ports.copy.ok,
      COPY_KEYS.ok,
      FALLBACK_COPY.ok,
    ),
    cancelText: null,
    cancelResult: false,
    okResult: () => true,
  });

  const showPrompt = (
    message: unknown,
    options: PromptDialogOptions = {},
  ): Promise<string | null> => showBasicDialog<string | null>({
    message,
    title: options.title,
    prompt: true,
    defaultValue: options.defaultValue ?? '',
    placeholder: options.placeholder || '',
    okText: options.okText || copyOrFallback(
      ports.copy.confirm,
      COPY_KEYS.confirm,
      FALLBACK_COPY.confirm,
    ),
    cancelText: options.cancelText || copyOrFallback(
      ports.copy.cancel,
      COPY_KEYS.cancel,
      FALLBACK_COPY.cancel,
    ),
    cancelResult: null,
    okResult: (input) => input?.value ?? '',
  });

  const showChoice = <Value extends string = string>(
    config: ChoiceDialogOptions<Value> = {},
  ): Promise<Value | null> => {
    const options = Array.isArray(config.options) ? config.options : [];
    const dismissValue = config.dismissValue != null
      ? config.dismissValue
      : options[0]?.value ?? null;
    if (destroyed) return Promise.resolve(dismissValue);
    replaceActiveDialog();
    const { overlay, card } = createCard(
      'app-dialog app-dialog-choice',
      'dialog',
      config.title,
      config.message,
    );
    const list = browserDocument.createElement('div');
    list.className = 'app-dialog-choices';
    for (const option of options) {
      const button = createActionButton(
        `app-choice-btn${option.accent ? ' is-accent' : ''}`,
        '',
      );
      if (option.icon) {
        const icon = browserDocument.createElement('span');
        icon.className = 'app-choice-icon icon-box';
        icon.innerHTML = option.icon;
        button.appendChild(icon);
      }
      const text = browserDocument.createElement('span');
      text.className = 'app-choice-text';
      const label = browserDocument.createElement('span');
      label.className = 'app-choice-label';
      label.textContent = option.label || option.value;
      text.appendChild(label);
      if (option.subtitle) {
        const subtitle = browserDocument.createElement('span');
        subtitle.className = 'app-choice-sub';
        subtitle.textContent = option.subtitle;
        text.appendChild(subtitle);
      }
      button.appendChild(text);
      list.appendChild(button);
    }
    card.appendChild(list);

    const session = createSession<Value | null>(overlay, dismissValue);
    const buttons = Array.from(
      list.querySelectorAll<HTMLButtonElement>('.app-choice-btn'),
    );
    buttons.forEach((button, index) => {
      button.addEventListener('click', () => {
        session.close(options[index]?.value ?? dismissValue);
      });
    });
    overlay.onclick = (event) => {
      if (event.target === overlay) session.close(dismissValue);
    };
    session.listenForKeys((event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        session.close(dismissValue);
        return;
      }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      if (buttons.length === 0) return;
      event.preventDefault();
      const currentIndex = buttons.indexOf(
        browserDocument.activeElement as HTMLButtonElement,
      );
      const step = event.key === 'ArrowDown' ? 1 : -1;
      const nextIndex = currentIndex < 0
        ? (step > 0 ? 0 : buttons.length - 1)
        : (currentIndex + step + buttons.length) % buttons.length;
      buttons[nextIndex]?.focus();
    });
    if (typeof config.liveCheck === 'function') {
      session.startLiveCheck(config.liveCheck);
    }
    session.startAnimation(() => {
      overlay.classList.add('open');
      buttons[0]?.focus();
    });
    return session.promise;
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    replaceActiveDialog();
  };

  return Object.freeze({
    showConfirm,
    showAlert,
    showPrompt,
    showChoice,
    destroy,
  });
}
