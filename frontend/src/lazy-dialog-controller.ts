/**
 * Responsibility: expose stable application-dialog services while loading the
 * DOM controller only for the first dialog request. Entry point:
 * createDialogServices. Dependencies: explicit controller ports and the lazy
 * DOM-controller module; tests may inject the loader at this boundary.
 */

import type {
  AlertDialogOptions,
  AppDialogController,
  AppDialogControllerPorts,
  ChoiceDialogOptions,
  ConfirmDialogOptions,
  PromptDialogOptions,
} from './dialog-controller';

export interface AppDialogControllerModule {
  createAppDialogController(ports: AppDialogControllerPorts): AppDialogController;
}

export type AppDialogControllerLoader = () => Promise<AppDialogControllerModule>;

export function createDialogServices(
  ports: AppDialogControllerPorts,
  loadController: AppDialogControllerLoader = () => import('./dialog-controller'),
): AppDialogController {
  let controllerPromise: Promise<AppDialogController | null> | null = null;
  let activeController: AppDialogController | null = null;
  let destroyed = false;

  const resolveController = (): Promise<AppDialogController | null> => {
    if (destroyed) return Promise.resolve(null);
    if (controllerPromise) return controllerPromise;
    controllerPromise = loadController()
      .then((module) => {
        if (destroyed) return null;
        const controller = module.createAppDialogController(ports);
        if (destroyed) {
          controller.destroy();
          return null;
        }
        activeController = controller;
        return controller;
      })
      .catch((error: unknown) => {
        controllerPromise = null;
        ports.log.warn('[AppDialog] controller load failed', error);
        return null;
      });
    return controllerPromise;
  };

  const invoke = async <Result>(
    fallback: Result,
    action: (controller: AppDialogController) => Promise<Result>,
  ): Promise<Result> => {
    const controller = await resolveController();
    return controller ? action(controller) : fallback;
  };

  const showConfirm = (
    message: unknown,
    options: ConfirmDialogOptions = {},
  ): Promise<boolean> => invoke(
    false,
    (controller) => controller.showConfirm(message, options),
  );

  const showAlert = (
    message: unknown,
    options: AlertDialogOptions = {},
  ): Promise<boolean> => invoke(
    false,
    (controller) => controller.showAlert(message, options),
  );

  const showPrompt = (
    message: unknown,
    options: PromptDialogOptions = {},
  ): Promise<string | null> => invoke(
    null,
    (controller) => controller.showPrompt(message, options),
  );

  const showChoice = <Value extends string = string>(
    options: ChoiceDialogOptions<Value> = {},
  ): Promise<Value | null> => {
    const choices = Array.isArray(options.options) ? options.options : [];
    const fallback = options.dismissValue != null
      ? options.dismissValue
      : choices[0]?.value ?? null;
    return invoke(
      fallback,
      (controller) => controller.showChoice(options),
    );
  };

  const destroy = (): void => {
    if (destroyed) return;
    destroyed = true;
    activeController?.destroy();
    activeController = null;
  };

  return Object.freeze({
    showConfirm,
    showAlert,
    showPrompt,
    showChoice,
    destroy,
  });
}
