"""Guard for contracts/ui_nav_targets_v1.json and its backend renderer.

The contract is generated (scripts/gen_ui_nav_targets.mjs) from the frontend
jump registries and owning panel labels. Freshness alone cannot detect a
wrong label association: the reviewed inventory below independently pins every
setting's label/description to its UI meaning. This suite also runs the DOM
association fixtures and pins the backend renderer's shape/degradation contract.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / 'contracts' / 'ui_nav_targets_v1.json'
KEBAB = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')


# Reviewed against the owning tab/control, NOT produced by the generator.
# New setting keys must receive an explicit semantic review here. Tuples are
# the ordered pieces of a composite label; rich catalog text is plain text in
# the model directory. Literal labels (MCP/API Key) are intentional UI copy.
SETTING_LABELS = {
    'general': 'settings.tabGeneral',
    'preset': 'settings.tabDisplay',
    'api': 'settings.tabProviders',
    'models': 'settings.tabModels',
    'oauth': 'settings.tabOAuth',
    'search': 'settings.tabSearch',
    'translate': 'settings.tabTranslate',
    'speech': 'settings.tabSpeech',
    'network': 'settings.tabNetwork',
    'devices': 'settings.tabDevices',
    'preferences': 'settings.tabPreferences',
    'skills': 'settings.tabSkills',
    'tools': 'settings.tabTools',
    'mcp': 'MCP',
    'experiments': 'settings.tabExperiments',
    'advanced': 'settings.tabAdvanced',
    'feishu': 'settings.tabFeishu',
    'language': 'settings.language',
    'system-prompt': 'settings.systemPrompt',
    'default-thinking-depth': 'settings.defaultThinkingDepth',
    'input-send-mode': 'settings.inputSendMode',
    'auto-generate-title': 'settings.autoGenerateTitle',
    'debug-mode': 'settings.debugMode',
    'optimizer': 'settings.optimizerModule',
    'trading': 'settings.tradingModule',
    'pptx-translate': 'settings.pptxTranslateModule',
    'default-model': 'settings.defaultModelFull',
    'fallback-model': 'settings.fallbackModelFull',
    'search-profile': 'settings.searchProfile',
    'search-deepen': 'settings.searchDeepen',
    'fetch-topn': 'settings.fetchTopNSimple',
    'fetch-timeout': 'settings.fetchTimeout',
    'llm-content-filter': 'settings.llmContentFilter',
    'skip-domains': 'settings.blockedDomains',
    'mt-enabled': 'settings.mtEnable',
    'mt-provider': 'settings.mtProvider',
    'mt-api-url': ('settings.mtApiUrlLabel', 'settings.mtApiUrlHint'),
    'mt-api-key': 'API Key',
    'mt-app-id': ('settings.mtAppIdLabel', 'settings.mtAppIdHint'),
    'stt-enabled': 'settings.sttEnable',
    'stt-provider': 'settings.sttProvider',
    'tool-search': 'settings.responsesToolSearch',
    'responses-reasoning': 'settings.responsesReasoning',
    'responses-transport': 'settings.responsesTransport',
    'responses-verbosity': 'settings.responsesVerbosity',
    'programmatic-calling': 'settings.responsesPtc',
    'pricing': 'settings.pricingOverride',
}
SETTING_DESCRIPTIONS = {
    'system-prompt': 'settings.systemPromptBlocksHint',
    'auto-generate-title': 'settings.autoGenerateTitleDesc',
    'debug-mode': 'settings.debugModeDesc',
    'optimizer': 'settings.optimizerModuleDesc',
    'trading': 'settings.tradingModuleDesc',
    'pptx-translate': 'settings.pptxTranslateModuleDesc',
    'search-profile': 'settings.searchProfileHint',
    'search-deepen': 'settings.searchDeepenDesc',
    'fetch-topn': 'settings.fetchTopNSimpleHint',
    'llm-content-filter': 'settings.llmContentFilterDesc',
    'skip-domains': 'settings.blockedDomainsDesc',
    'mt-enabled': 'settings.mtEnableDesc',
    'stt-enabled': 'settings.sttEnableDesc',
    'pricing': 'settings.pricingOverrideDesc',
}


class _CatalogText(HTMLParser):
    """Independent normalization of the audited rich-label catalog entries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append(' ')


def _expected_text(source, locale):
    catalog = json.loads((ROOT / 'frontend/src/i18n/locales' / f'{locale}.json').read_text())
    pieces = source if isinstance(source, tuple) else (source,)
    parser = _CatalogText()
    parser.feed(' '.join(catalog[piece] if piece.startswith('settings.') else piece
                         for piece in pieces))
    return ' '.join(''.join(parser.parts).split())


@pytest.mark.parametrize('key', SETTING_LABELS)
def test_every_setting_has_its_own_semantic_label_and_description(key):
    target = next(t for t in _load()['targets'] if t['scheme'] == 'setting' and t['key'] == key)
    assert target['name'] == {
        locale: _expected_text(SETTING_LABELS[key], locale) for locale in ('zh', 'en')
    }
    description = SETTING_DESCRIPTIONS.get(key)
    if description:
        assert target.get('description') == {
            locale: _expected_text(description, locale) for locale in ('zh', 'en')
        }
    else:
        assert 'description' not in target, f'{key} borrowed an unrelated description'


def test_semantic_audit_covers_exactly_the_emitted_settings():
    assert {t['key'] for t in _load()['targets'] if t['scheme'] == 'setting'} == set(SETTING_LABELS)


@pytest.mark.parametrize('args', [
    ['scripts/gen_ui_nav_targets.mjs', '--check'],
    ['--test', 'tests/ui_nav/ui_nav_generator.test.mjs'],
], ids=['freshness', 'structural-label-association'])
def test_generator_checks(args):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for navigation generator checks')
    result = subprocess.run([node, *args], cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def _load() -> dict:
    return json.loads(CONTRACT.read_text(encoding='utf-8'))


def test_contract_header_and_link_syntax():
    doc = _load()
    assert doc['contract'] == 'tofu.ui-nav-targets/v1'
    assert set(doc['linkSyntax']) == {'setting', 'page'}
    for syntax in doc['linkSyntax'].values():
        assert '<key>' in syntax


def test_targets_are_unique_kebab_and_bilingual():
    doc = _load()
    targets = doc['targets']
    assert targets, 'directory must not be empty'
    keys = [t['key'] for t in targets]
    assert len(keys) == len(set(keys))
    for target in targets:
        assert KEBAB.match(target['key']), target['key']
        assert target['scheme'] in ('setting', 'page')
        name = target.get('name') or {}
        assert name.get('zh') and name.get('en'), target['key']
        if target['scheme'] == 'setting':
            assert target.get('tab'), target['key']
        if target.get('tabLevel'):
            assert 'description' not in target


def test_flag_gated_targets_are_explicit():
    for target in _load()['targets']:
        if 'requiresFlag' in target:
            assert target['scheme'] == 'page'
            assert target['requiresFlag'] == 'debug_mode'


def test_registry_spec_and_renderer():
    from lib.tasks_pkg.context_composer.registry import injection_spec
    from lib.ui_nav import build_ui_nav_directory

    spec = injection_spec('ui_nav_directory')
    assert spec.stability == 'static'
    assert spec.max_tokens and spec.max_tokens >= 1000
    # Registry-declared system prefix (browser-frontend turns only); the
    # directory never rides a tail user carrier.
    assert spec.placement == 'system'

    content = build_ui_nav_directory()
    assert 'setting:debug-mode' not in content  # keys render as `(setting:<key>)` syntax lines
    assert '`debug-mode`' in content
    assert '界面导航目录' in content
    assert '(setting:<key>)' in content and '(page:<key>)' in content
    # Every contract key must appear in the rendered directory.
    for target in _load()['targets']:
        assert f'`{target["key"]}`' in content, target['key']

    # The actual injected directory must carry the reviewed meaning, not just
    # a valid key copied from an internally consistent but wrong artifact.
    for key, source in SETTING_LABELS.items():
        assert f'`{key}` {_expected_text(source, "zh")}' in content, key

def test_renderer_truncates_descriptions_and_degrades(monkeypatch):
    import lib.ui_nav as ui_nav

    long_desc = 'x' * 200
    fake = {
        'targets': [
            {'key': 'demo', 'scheme': 'setting',
             'name': {'zh': '演示', 'en': 'Demo'},
             'description': {'zh': long_desc}},
        ],
    }
    monkeypatch.setattr(ui_nav, '_contract', lambda: fake)
    content = ui_nav.build_ui_nav_directory()
    assert long_desc not in content
    assert '…' in content

    monkeypatch.setattr(ui_nav, '_contract', lambda: {})
    assert ui_nav.build_ui_nav_directory() == ''

    def _boom():
        raise OSError('missing')
    monkeypatch.setattr(ui_nav, '_contract', _boom)
    assert ui_nav.build_ui_nav_directory() == ''
