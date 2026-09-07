/**
 * Responsibility: own the JS side of the Android native WebView shell
 * contract — the `tofu:native-visibility` lifecycle signal and the
 * rate-limited `TofuNative.requestReauth` escape hatch. Entry points:
 * createNativeVisibility, createNativeReauthGate, isGatewayAuthRejection.
 * Dependencies: injected event-subscription, document-hidden probe, native
 * handle, and clock ports; no DOM or timer access of its own.
 *
 * A WebView keeps document.visibilityState === 'visible' while the app is
 * backgrounded, so every foreground-cadence budget layer (push ping, catalog
 * reconcile, inspector polling, elapsed tickers) would keep hammering the
 * vscode proxy tunnel from a pocket. The shell dispatches
 * `tofu:native-visibility` from ON_START/ON_STOP; this owner folds it with
 * the document state into one effective-hidden predicate those layers share.
 *
 * The other direction: when the outer gateway's session cookie dies while
 * the page stays open, API calls bounce with the edge's own 401
 * (`{"error":"Unauthorized"}` — error as a bare string, never Tofu's
 * `{"ok":false,"error":{…}}` envelope). Only the shell can re-login
 * headlessly, so the transport forwards those rejections through the reauth
 * gate; the shell caps consecutive failures on its side.
 */

export const NATIVE_BRIDGE_POLICY = Object.freeze({
  visibilityEvent: 'tofu:native-visibility',
  reauthMinIntervalMs: 30_000,
  reasonMaxLength: 120,
});

export interface NativeShellHandle {
  requestReauth?: (reason: string) => void;
}

export interface NativeVisibilityPorts {
  /** Attach the shell's visibility event; the listener receives `hidden`. */
  subscribeNativeVisibility(listener: (hidden: boolean) => void): void;
  documentHidden(): boolean;
  native?: NativeShellHandle | null;
  onError?(error: unknown): void;
}

export interface NativeVisibility {
  /** True when the Android shell's bridge object is present. */
  isNativeShell(): boolean;
  /** True when the shell reported the app backgrounded. */
  isHidden(): boolean;
  /** Document-hidden OR shell-hidden: the predicate budget layers must use. */
  isEffectivelyHidden(): boolean;
  /** Fired after a shell flip changes the effective state; returns unsubscribe. */
  subscribe(listener: (effectivelyHidden: boolean) => void): () => void;
}

export interface NativeReauthGatePorts {
  native?: NativeShellHandle | null;
  now(): number;
  minIntervalMs?: number;
  onError?(error: unknown): void;
}

export interface NativeReauthGate {
  available(): boolean;
  /**
   * Ask the shell to re-establish the gateway session. Repeat calls inside
   * the window are dropped so a burst of 401ing polls cannot storm the
   * login endpoint. Returns true when the call was forwarded to the shell.
   */
  requestReauth(reason: unknown): boolean;
}

/**
 * True when an HTTP failure is the OUTER gateway refusing the request, not
 * Tofu: the code-server edge answers `{"error":"Unauthorized"}` (error as a
 * bare string), never Tofu's envelope. Mirrors the Android probe's GATEWAY
 * verdict (session/TofuProbe.kt) — same wire contract, same discrimination,
 * so a 401/403 carrying a Tofu envelope or problem detail never triggers a
 * shell re-login.
 */
export function isGatewayAuthRejection(
  status: number,
  envelope: unknown,
  problem: unknown,
): boolean {
  return (status === 401 || status === 403) && envelope == null && problem == null;
}

function normalizeReason(value: unknown): string {
  const reason = typeof value === 'string' ? value.trim() : '';
  if (!reason) return 'unknown';
  return reason.slice(0, NATIVE_BRIDGE_POLICY.reasonMaxLength);
}

export function createNativeVisibility(
  ports: NativeVisibilityPorts,
): NativeVisibility {
  const listeners = new Set<(effectivelyHidden: boolean) => void>();
  let nativeHidden = false;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // Visibility reporting is best-effort; it must never break its consumer.
    }
  };

  const documentHidden = (): boolean => {
    try {
      return ports.documentHidden() === true;
    } catch (error: unknown) {
      reportError(error);
      return false;
    }
  };

  const isEffectivelyHidden = (): boolean => nativeHidden || documentHidden();

  const emitIfFlipped = (previous: boolean): void => {
    const current = isEffectivelyHidden();
    if (current === previous) return;
    for (const listener of Array.from(listeners)) {
      try {
        listener(current);
      } catch (error: unknown) {
        reportError(error);
      }
    }
  };

  try {
    ports.subscribeNativeVisibility((hidden: unknown) => {
      const previous = isEffectivelyHidden();
      nativeHidden = hidden === true;
      emitIfFlipped(previous);
    });
  } catch (error: unknown) {
    reportError(error);
  }

  return {
    isNativeShell(): boolean {
      return typeof ports.native?.requestReauth === 'function';
    },
    isHidden(): boolean {
      return nativeHidden;
    },
    isEffectivelyHidden,
    subscribe(listener: (effectivelyHidden: boolean) => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

export function createNativeReauthGate(
  ports: NativeReauthGatePorts,
): NativeReauthGate {
  const minIntervalMs = Number.isFinite(ports.minIntervalMs)
    ? Math.max(0, Number(ports.minIntervalMs))
    : NATIVE_BRIDGE_POLICY.reauthMinIntervalMs;
  let lastForwardedAt = Number.NEGATIVE_INFINITY;

  const reportError = (error: unknown): void => {
    try {
      ports.onError?.(error);
    } catch {
      // The escape hatch must never break the caller's error path.
    }
  };

  return {
    available(): boolean {
      return typeof ports.native?.requestReauth === 'function';
    },
    requestReauth(reason: unknown): boolean {
      const forward = ports.native?.requestReauth;
      if (typeof forward !== 'function') return false;
      let now = 0;
      try {
        const value = ports.now();
        now = Number.isFinite(value) ? value : 0;
      } catch (error: unknown) {
        reportError(error);
      }
      if (now - lastForwardedAt < minIntervalMs) return false;
      try {
        forward.call(ports.native, normalizeReason(reason));
      } catch (error: unknown) {
        reportError(error);
        return false;
      }
      lastForwardedAt = now;
      return true;
    },
  };
}
