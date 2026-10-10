"""Declared same-origin JSON reads using credentials inside the user's page.

Recipes are adapter-owned Python values, never model-supplied URLs, headers or
credential selectors. Cookies remain browser-managed. Optional storage tokens
are read and used in one page execution, scrubbed before return, never cached.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AuthenticatedRead:
    origin: str
    path: str
    query_parameters: tuple[str, ...] = ()
    token_storage: str = ''
    token_key: str = ''
    token_header: str = 'Authorization'
    token_prefix: str = 'Bearer '

    def __post_init__(self):
        parsed = urlsplit(self.origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('Authenticated read origin must be an HTTP(S) origin')
        if (not self.path.startswith('/') or self.path.startswith('//')
                or any(char in self.path for char in ('?', '#', '\\'))
                or '..' in self.path or len(self.path) > 1024):
            raise ValueError('Authenticated read path must be a fixed same-origin path')
        if self.token_storage not in ('', 'localStorage', 'sessionStorage'):
            raise ValueError('Unsupported browser credential storage')
        if self.token_storage and (not self.token_key or len(self.token_key) > 128):
            raise ValueError('A bounded adapter-declared token key is required')
        if (not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', self.token_header)
                or self.token_header.lower() in ('cookie', 'host', 'origin', 'referer')
                or '\r' in self.token_prefix or '\n' in self.token_prefix
                or len(self.token_prefix) > 32):
            raise ValueError('Invalid credential header')
        if len(self.query_parameters) > 16 or any(
                not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key) for key in self.query_parameters):
            raise ValueError('Query parameter names must be bounded and explicit')

    def execute(self, page, query):
        from .access import require_access
        if not isinstance(query, dict) or set(query) - set(self.query_parameters):
            raise ValueError('Undeclared authenticated request query parameter')
        normalized = {}
        total = 0
        for key, value in query.items():
            values = value if isinstance(value, list) else [value]
            if len(values) > 64 or any(not isinstance(item, (str, int, float, bool)) for item in values):
                raise ValueError('Query values must be bounded scalar lists')
            normalized[key] = [str(item) for item in values]
            total += sum(len(item) for item in normalized[key])
        if total > 8192:
            raise ValueError('Authenticated query exceeds the 8192-character budget')
        require_access(page.lease.owner_user_id, self.origin + self.path,
                       access='read', client_id=page.lease.client_id, profile=page.lease.profile)
        receipt = page.execute(_READ_JSON, args={
            'origin': self.origin, 'path': self.path, 'query': normalized,
            'tokenStorage': self.token_storage, 'tokenKey': self.token_key,
            'tokenHeader': self.token_header, 'tokenPrefix': self.token_prefix,
        }, trusted_read=True)
        from .adapters import _extract_result
        return _extract_result(receipt)


_READ_JSON = r"""
(async () => {
  if (location.origin !== args.origin) return {ok: false, reason: 'origin_changed'};
  const url = new URL(args.path, args.origin);
  if (url.origin !== location.origin) return {ok: false, reason: 'origin_changed'};
  for (const [name, values] of Object.entries(args.query)) {
    for (const value of values) url.searchParams.append(name, value);
  }
  const headers = {accept: 'application/json'};
  let token = '';
  if (args.tokenStorage) {
    try { token = window[args.tokenStorage].getItem(args.tokenKey) || ''; }
    catch (_) { return {ok: false, reason: 'credential_unavailable'}; }
    if (typeof token !== 'string' || !token || token.length > 16384) return {ok: false, reason: 'login_required'};
    headers[args.tokenHeader] = args.tokenPrefix + token;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  let reader;
  try {
    const response = await fetch(url.href, {method: 'GET', credentials: 'same-origin',
      redirect: 'error', headers, signal: controller.signal});
    if ([401, 403].includes(response.status)) return {ok: false, status: response.status, reason: 'login_required'};
    if (!response.ok) return {ok: false, status: response.status, reason: 'http_error'};
    if (new URL(response.url).origin !== args.origin) return {ok: false, reason: 'origin_changed'};
    if (!/json/i.test(response.headers.get('content-type') || '')) return {ok: false, reason: 'not_json'};
    reader = response.body?.getReader();
    if (!reader) return {ok: false, reason: 'empty_body'};
    const decoder = new TextDecoder();
    let text = '', bytes = 0;
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      bytes += chunk.value.byteLength;
      if (bytes > 262144) return {ok: false, reason: 'response_budget'};
      text += decoder.decode(chunk.value, {stream: true});
    }
    let nodes = 0;
    const secretKey = /^(token|session|jwt|api_?key)$|access.?token|refresh.?token|authorization|password|secret|cookie|credential|session.?id/i;
    const clean = (value, depth = 0) => {
      if (++nodes > 4000 || depth > 16) throw new Error('response_budget');
      if (typeof value === 'string') {
        let out = token ? value.split(token).join('[redacted]') : value;
        return out.replace(/Bearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [redacted]');
      }
      if (Array.isArray(value)) return value.map(item => clean(item, depth + 1));
      if (value && typeof value === 'object') {
        const result = Object.create(null);
        for (const [key, item] of Object.entries(value)) {
          result[clean(key, depth + 1)] = secretKey.test(key) ? '[redacted]' : clean(item, depth + 1);
        }
        return result;
      }
      return value;
    };
    const body = clean(JSON.parse(text + decoder.decode()));
    if (JSON.stringify(body).length > 262144) return {ok: false, reason: 'response_budget'};
    if (location.origin !== args.origin) return {ok: false, reason: 'origin_changed'};
    return {ok: true, status: response.status, body};
  } catch (_) {
    return {ok: false, reason: controller.signal.aborted ? 'timeout' : 'request_failed'};
  } finally {
    clearTimeout(timer);
    if (reader) await reader.cancel().catch(() => {});
    token = '';
    delete headers[args.tokenHeader];
  }
})()
"""
