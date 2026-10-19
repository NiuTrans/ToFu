/** Best-effort notifications only; required projection/commit ports must propagate failure. */
export function notifyObserver<Args extends unknown[]>(
  name: string, callback: ((...args: Args) => void) | undefined, ...args: Args
): void {
  try {
    callback?.(...args);
  } catch (error) {
    console.warn(`[${name}] observer failed`, error);
  }
}
