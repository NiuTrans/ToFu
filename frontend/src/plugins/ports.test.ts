// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { createPluginHost } from './ports';

afterEach(() => vi.unstubAllGlobals());

it('shares the current API transport and unwraps the response under a proxy prefix', async () => {
  window.history.replaceState({}, '', '/proxy/15000/trading.html');
  const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({
    data: { holdings: [{ symbol: 'test' }] }, meta: { requestId: 'test' },
  }), { headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetch);
  const host = createPluginHost(document);
  expect(await host.api.trading.call('/holdings')).toMatchObject({ holdings: [{ symbol: 'test' }] });
  expect(String(fetch.mock.calls[0]?.[0])).toBe('/proxy/15000/api/v4/trading/holdings');
});

it('parses formatted Markdown and sanitizes executable content with the shared dependency', () => {
  const host = createPluginHost(document);
  const element = document.createElement('div');
  element.innerHTML = host.markdown.sanitize(host.markdown.parse(
    '**Readable**\n\n<img src="x" onerror="alert(1)"><script>alert(1)</script>',
  ));
  expect(element.querySelector('strong')?.textContent).toBe('Readable');
  expect(element.querySelector('[onerror],script')).toBeNull();
  expect(host.version).toBe(1);
  expect(Object.isFrozen(host)).toBe(true);
});

it('keeps backend error details and request identity through the common transport', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    type: 'urn:tofu:problem:bad_request', title: 'Bad request', status: 400,
    detail: 'symbol required', instance: '/api/v4/trading/holdings',
    code: 'bad_request', requestId: 'plugin-request',
  }), { status: 400, headers: { 'Content-Type': 'application/problem+json' } })));
  await expect(createPluginHost(document).api.trading.call('/holdings', {
    method: 'POST', body: '{}',
  })).rejects.toMatchObject({
    name: 'ApiError', status: 400, code: 'bad_request',
    requestId: 'plugin-request', message: 'symbol required',
  });
});

it('leaves the raw streaming body with its caller', async () => {
  const response = new Response('data: {"thinking":"working"}\n\ndata: {"done":true}\n\n', {
    headers: { 'Content-Type': 'text/event-stream' },
  });
  vi.stubGlobal('fetch', vi.fn(async () => response));
  const raw = await createPluginHost(document).api.trading.raw('/brain/stream', {
    method: 'POST', body: '{}',
  });
  expect(raw?.bodyUsed).toBe(false);
  expect(await raw?.text()).toBe('data: {"thinking":"working"}\n\ndata: {"done":true}\n\n');
});
