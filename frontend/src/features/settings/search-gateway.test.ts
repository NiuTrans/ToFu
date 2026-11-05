// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from 'vitest';
/* The 搜索 API card renders from the server projection and produces the
 * `search.gateway` save payload. It runs against the shipped panel markup so
 * a renamed id fails here instead of silently dropping a setting. */
import searchPanel from '../../../../static/settings_panels/search.html?raw';
import {
  collectSearchGatewayPayload,
  markSearchGatewayKeySaved,
  renderSearchGateway,
  searchGatewayChanged,
} from './search-gateway';

const catalog = [{
  id: 'alpha',
  name: 'Alpha 搜索',
  key_label: '计费 AppId',
  key_hint: '与模型调用相同的 AppId。',
  sources: [
    { id: 'one-search', name: 'One', default: true },
    { id: 'two-search', name: 'Two', default: false, note: '限流' },
  ],
}, {
  id: 'beta',
  name: 'Beta',
  sources: [{ id: 'three-search', name: 'Three', default: true }],
}];

function element<T extends HTMLElement>(id: string): T {
  return document.getElementById(id) as T;
}

function checkedSources(): string[] {
  return Array.from(document.querySelectorAll<HTMLInputElement>(
    '#settingSearchGatewaySources input:checked')).map((input) => input.value);
}

beforeEach(() => {
  document.body.innerHTML = searchPanel;
});

describe('search gateway settings card', () => {
  it('stays hidden and out of the payload without a catalog or saved gateway', () => {
    renderSearchGateway({ gateway: { enabled: false }, gateway_catalog: [] });
    expect(element('searchGatewayCard').hidden).toBe(true);
    expect(collectSearchGatewayPayload()).toBeNull();
  });

  it('renders saved choices and never pre-fills the key', () => {
    renderSearchGateway({
      gateway: { enabled: true, id: 'alpha', sources: ['two-search'], free_engines: 'off',
                 full_content: true, has_key: true, key_hint: '2031…4861' },
      gateway_catalog: catalog,
    });
    expect(element('searchGatewayCard').hidden).toBe(false);
    expect(checkedSources()).toEqual(['two-search']);
    const key = element<HTMLInputElement>('settingSearchGatewayKey');
    expect(key.value).toBe('');
    expect(key.placeholder).toContain('2031…4861');
    expect(element('settingSearchGatewayKeyLabel').textContent).toBe('计费 AppId');
    expect(element('searchGatewayStatus').textContent).toContain('1 个来源');
    expect(collectSearchGatewayPayload()).toEqual({
      enabled: true, id: 'alpha', sources: ['two-search'], free_engines: 'off', full_content: true,
    });
  });

  it('switching gateways selects that gateway\'s default sources', () => {
    renderSearchGateway({ gateway: { id: 'alpha', sources: ['two-search'] }, gateway_catalog: catalog });
    element<HTMLSelectElement>('settingSearchGatewayId').value = 'beta';
    searchGatewayChanged();
    expect(checkedSources()).toEqual(['three-search']);
  });

  it('sends a typed key once and clears it after a successful save', () => {
    renderSearchGateway({ gateway: { enabled: true, id: 'alpha' }, gateway_catalog: catalog });
    expect(checkedSources()).toEqual(['one-search']);
    expect(element('searchGatewayStatus').textContent).toContain('API Key');
    element<HTMLInputElement>('settingSearchGatewayKey').value = '  secret-key  ';
    const payload = collectSearchGatewayPayload();
    expect(payload?.api_key).toBe('secret-key');
    markSearchGatewayKeySaved(payload!);
    expect(element<HTMLInputElement>('settingSearchGatewayKey').value).toBe('');
    expect(collectSearchGatewayPayload()?.api_key).toBeUndefined();
  });

  it('round-trips a saved custom gateway endpoint without a catalog', () => {
    renderSearchGateway({
      gateway: { enabled: true, endpoint: 'https://gateway.example.test/search', sources: ['x-search'] },
      gateway_catalog: [],
    });
    expect(element('searchGatewayCard').hidden).toBe(false);
    expect(element<HTMLSelectElement>('settingSearchGatewayId').selectedOptions[0].textContent)
      .toContain('gateway.example.test');
    expect(collectSearchGatewayPayload()).toMatchObject({
      id: '', endpoint: 'https://gateway.example.test/search', sources: ['x-search'],
    });
  });
});
