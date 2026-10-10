/** Subscription login reuse transport. Contract: docs/IDENTITY.md. */
import { ApiError, request } from './transport';

export interface CodexLoginStatus {
  authenticated?: boolean;
  status?: string;
  auth_issue?: string;
  credential_source?: string;
  shared_login_reuse_supported?: boolean;
}

/** Explicit reuse never falls back to starting a new OAuth authorization. */
export async function reuseCodexLogin(): Promise<CodexLoginStatus> {
  const params = { provider: 'codex', reuse_only: true };
  try {
    try {
      return await request('/api/oauth/login', {
        method: 'POST', json: params, timeout: 20000,
      });
    } catch (error) {
      // Match the browser login's constrained-proxy fallback. Preserve the
      // reuse-only flag on GET so a tunnel cannot start a fresh authorization.
      if (!(error instanceof ApiError) || ![404, 405].includes(error.status)) throw error;
      return await request('/api/oauth/login', { query: params, timeout: 20000 });
    }
  } catch (error) {
    // A pinned source failure is HTTP 400, with a redacted auth_issue. Keep
    // that diagnosis for the card; other transport/authority failures throw.
    const body = error instanceof ApiError && error.status === 400 ? error.body : null;
    if (body && typeof body === 'object' && !Array.isArray(body)) {
      const result = body as Record<string, unknown>;
      if (result.status === 'error' && typeof result.auth_issue === 'string' && result.auth_issue) {
        return { status: 'error', auth_issue: result.auth_issue };
      }
    }
    throw error;
  }
}
