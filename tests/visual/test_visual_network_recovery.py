"""Network recovery labels through the real app and production renderer."""
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from lib.llm_dispatch.retry_i18n import retry_phase_fields
from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('language', ['zh', 'en'])
@pytest.mark.parametrize('width', [390, 1280])
def test_network_wait_recovers_without_failure_or_switch_claim(page, tmp_path, language, width):
    page.set_viewport_size({'width': width, 'height': 900})
    locale = json.loads((ROOT / f'frontend/src/i18n/locales/{language}.json').read_text())
    page.add_script_tag(path=native_module_path('.native/network-recovery-renderers.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts'))
    page.evaluate(r"""locale => {
      const node = document.createElement('div');
      node.id = 'network-recovery-fixture';
      node.style.cssText = 'position:fixed;inset:20px 12px auto;z-index:99999;background:var(--bg-primary);padding:12px';
      document.body.append(node);
      const text = (key, fallback, values) => {
        const args = {...(values || {})};
        if (args.reasonKey && locale[args.reasonKey]) args.reason = locale[args.reasonKey];
        return (locale[key] || fallback).replace(/\{(\w+)\}/g, (m,k) => args[k] ?? m);
      };
      const renderers = window.createClassicConversationRenderers({
        renderSafeMarkdownHtml: value => value, localizedText: text,
      });
      window.paintNetworkRecovery = value => renderers.renderBlock(node,
        {kind:'live-status',blockId:'network-recovery',value},{});
    }""", locale)
    fields = retry_phase_fields(model='gpt-6-astra', attempt=0,
                                reason='Waiting for network recovery')
    page.evaluate('fields => paintNetworkRecovery({phase:"retrying",...fields})', fields)
    node = page.locator('#network-recovery-fixture')
    expect(node).to_contain_text(locale['stream.retryReason.networkRecovery'])
    text = node.inner_text()
    for unexpected in ('EndpointUnreachableError', '第 0 次', 'attempt 0', '切换通道', 'switching channel', '请求失败'):
        assert unexpected not in text
    box = node.bounding_box()
    assert box and 0 <= box['x'] and box['x'] + box['width'] <= width
    page.screenshot(path=str(tmp_path / f'network-recovery-{language}-{width}.png'))
    recovered = retry_phase_fields(model='gpt-6-astra', attempt=0, reason='Network recovered')
    page.evaluate('fields => paintNetworkRecovery({phase:"retrying",...fields})', recovered)
    expect(node).not_to_contain_text(locale['stream.retryReason.networkRecovery'])
    expect(node).to_contain_text(locale['stream.phase.waitingForModel'].replace('{model}', 'gpt-6-astra'))
    page.evaluate("paintNetworkRecovery({phase:'llm_thinking',detailKey:'stream.phase.generatingResponse'})")
    expect(node).to_contain_text(locale['stream.phase.generatingResponse'])
    expect(node).not_to_contain_text('gpt-6-astra')


@pytest.mark.parametrize('width', [390, 1280])
def test_long_request_model_name_wraps_in_full(page, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.add_script_tag(path=native_module_path('.native/network-recovery-renderers.js',
        ROOT / 'frontend/src/conversation/ui/classic-conversation-renderers.ts'))
    model = 'kunlunbedrock.claude-opus-4.5-m17-' + 'very-long-deployment-suffix-' * 4 + 'end'
    page.evaluate(r"""() => {
      const node = document.createElement('div');
      node.id = 'long-model-fixture';
      node.style.cssText = 'position:fixed;inset:20px 12px auto;z-index:99999;background:var(--bg-primary);padding:12px';
      document.body.append(node);
      const renderers = window.createClassicConversationRenderers({
        renderSafeMarkdownHtml: value => value,
        localizedText: (key, fallback, values) => fallback.replace(/\{(\w+)\}/g, (m, k) => (values || {})[k] ?? m),
      });
      window.paintLongModel = value => renderers.renderBlock(node,
        {kind:'live-status',blockId:'long-model',value},{});
    }""")
    page.evaluate('model => paintLongModel({phase:"waiting_model",detailKey:"stream.phase.waitingForModel",'
                  'detail:`Sent to ${model}, waiting for it to start replying…`,detailArgs:{model}})', model)
    text = page.locator('#long-model-fixture .stream-phase-text')
    expect(text).to_contain_text(model)
    metrics = text.evaluate("""el => {
      const style = getComputedStyle(el);
      return {scroll: el.scrollWidth, client: el.clientWidth, height: el.getBoundingClientRect().height,
              line: parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5,
              right: el.getBoundingClientRect().right};
    }""")
    assert metrics['scroll'] <= metrics['client'] + 1
    assert metrics['right'] <= width
    assert metrics['height'] > metrics['line'] * 1.5
