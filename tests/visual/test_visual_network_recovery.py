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
