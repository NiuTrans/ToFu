"""Derived UI locales: translation tool, generator projection and manifest consumers.

The language manifest (frontend/src/i18n/languages.json) declares source
locales that developers author and derived locales produced by
scripts/translate_i18n_locales.py. These specifications pin the safety rules
a derived translation must satisfy, how pending work is detected (missing,
stale source fingerprint, invalid), how delivery completes pending entries
from the fallback locale, and that every server consumer follows the manifest.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / 'scripts' / 'translate_i18n_locales.py'
GENERATOR_PATH = ROOT / 'scripts' / 'gen_i18n_contract.mjs'
MANIFEST = json.loads((ROOT / 'frontend/src/i18n/languages.json').read_text(encoding='utf-8'))


def _tool():
    spec = importlib.util.spec_from_file_location('translate_i18n_locales', TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _tool()


def _errors(language: str, text: str, zh: str, en: str) -> list[str]:
    return TOOL.validate_translation(language, text, {'zh': zh, 'en': en}, 'en')[0]


# ── Mechanical validation ────────────────────────────────────────────────

@pytest.mark.parametrize(('language', 'text', 'problem'), [
    ('de', '{n} Datei{s} geändert', 'forbidden placeholders'),
    ('ru', 'Изменено файлов', 'missing placeholders'),
    ('ja', '{n}件{x}', 'forbidden placeholders'),
    ('fr', '{n} fichier{s} modifié{s}', 'more often than in the source'),
])
def test_placeholders_are_runtime_safe(language, text, problem):
    errors = _errors(language, text, '{n} 个文件已修改', '{n} file{s} changed')
    assert any(problem in error for error in errors), errors


def test_plural_suffix_is_a_per_language_policy():
    zh, en = '{n} 次后', 'after {n} poll{s}'
    assert _errors('fr', 'après {n} vérification{s}', zh, en) == []
    assert _errors('de', 'nach {n} Abfrage{s}', zh, en)
    assert _errors('ru', 'Опросов: {n}', zh, en) == []


@pytest.mark.parametrize(('language', 'text', 'zh', 'en', 'problem'), [
    ('fr', 'Cliquez ici', '点击<b>这里</b>', 'Click <b>here</b>', 'HTML tags'),
    ('ru', 'Строка1 Строка2', '行1\n行2', 'Line1\nLine2', 'line-break'),
    ('fr', '· {n} éléments', ' · {n} 项', ' · {n} items', 'leading whitespace'),
    ('de', 'Siehe Dokumentation', '见 https://x.test/docs', 'See https://x.test/docs', 'URLs'),
    ('fr', '保存', '保存', 'Save', 'Chinese characters'),
    ('ko', 'Save the file now', '立即保存文件', 'Save the file now', 'no ko script'),
    ('de', 'Save the file now', '立即保存文件', 'Save the file now', 'identical to the English'),
    ('ru', 'x', '', '', 'must be empty'),
    ('ja', '', '保存', 'Save', 'empty translation'),
])
def test_structural_and_untranslated_defects_are_rejected(language, text, zh, en, problem):
    errors = _errors(language, text, zh, en)
    assert any(problem in error for error in errors), errors


@pytest.mark.parametrize(('language', 'text', 'zh', 'en'), [
    ('fr', 'Cliquez <b>ici</b>', '点击<b>这里</b>', 'Click <b>here</b>'),
    ('de', 'Details', '详情', 'Details'),
    ('ja', 'API Key', 'API Key', 'API Key'),
    ('ja', 'API URL', 'API 地址', 'API URL'),
    ('ko', 'API URL', 'API 地址', 'API URL'),
    ('ja', '', '周', ''),
    ('ru', 'Строка1\nСтрока2', '行1\n行2', 'Line1\nLine2'),
])
def test_correct_translations_pass(language, text, zh, en):
    assert _errors(language, text, zh, en) == []


def test_every_derived_language_has_a_translation_policy():
    derived = [entry['code'] for entry in MANIFEST['languages']
               if entry['code'] not in MANIFEST['sources']]
    assert set(derived) <= set(TOOL.LANGUAGE_POLICIES)


# ── Planning: missing, stale and invalid work ────────────────────────────

def _manifest_with(*codes):
    languages = tuple(TOOL.Language(code, code, code, code) for code in ('zh', 'en', *codes))
    return TOOL.Manifest(languages, ('zh', 'en'), 'en')


def test_plan_detects_missing_stale_invalid_and_extra_keys():
    manifest = _manifest_with('fr')
    sources = {'zh': {'a.one': '一', 'a.two': '{n} 个', 'a.three': '三'},
               'en': {'a.one': 'One', 'a.two': '{n} items', 'a.three': 'Three'}}
    keys = list(sources['zh'])
    fingerprints = {key: TOOL.source_fingerprint(key, sources, manifest) for key in keys}
    fingerprints['a.three'] = 'edited-source'
    derived = {'fr': {'a.two': 'éléments', 'a.three': 'Trois', 'a.gone': 'Supprimé'}}
    plan = TOOL.plan_work(manifest, keys, sources, derived, fingerprints, set(),
                          [manifest.language('fr')])
    assert plan.work['fr'] == ['a.one', 'a.two', 'a.three']
    assert plan.reasons['fr'] == {'missing': 1, 'invalid': 1, 'stale': 1}
    assert plan.extras['fr'] == ['a.gone']
    assert TOOL.report_check(manifest, plan) == 1


def test_fingerprint_tracks_every_source_language():
    manifest = _manifest_with('ja')
    sources = {'zh': {'k': '保存'}, 'en': {'k': 'Save'}}
    before = TOOL.source_fingerprint('k', sources, manifest)
    sources['en']['k'] = 'Save all'
    assert TOOL.source_fingerprint('k', sources, manifest) != before


def test_batches_keep_screens_together_and_chain_split_namespaces():
    manifest = _manifest_with('ja')
    keys = [f'big.k{i}' for i in range(TOOL.BATCH_ENTRY_LIMIT + 5)] + ['small.a', 'small.b']
    sources = {code: {key: 'x' for key in keys} for code in ('zh', 'en')}
    batches = TOOL.make_batches(keys, sources, manifest)
    assert [len(batch) for batch in batches] == [TOOL.BATCH_ENTRY_LIMIT, 7]
    chains = TOOL.make_chains(batches)
    assert len(chains) == 1  # the second batch continues the split namespace


def test_model_replies_are_parsed_from_fenced_or_wrapped_json():
    assert TOOL.parse_json_object('```json\n{"a": "b"}\n```') == {'a': 'b'}
    assert TOOL.parse_json_object('Here: {"a": "b"} done') == {'a': 'b'}
    # Trailing prose may itself contain braces (placeholders such as {n}).
    assert TOOL.parse_json_object('{}\n\nAll good; placeholders `{a}` and {b} are kept.') == {}
    assert TOOL.parse_json_object('Result: {"k": "{n} files"} — kept {n}') == {'k': '{n} files'}
    with pytest.raises(TOOL.TranslationToolError):
        TOOL.parse_json_object('no json here')


# ── Pipeline orchestration with a scripted model ─────────────────────────

class _ScriptedClient:
    """Answers translate/review/adjudicate requests in call order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def complete_json(self, model, system, user):
        self.requests.append((model, system, user))
        return self.replies.pop(0)


def test_pipeline_retries_invalid_output_then_applies_adjudicated_review():
    manifest = _manifest_with('de')
    sources = {'zh': {'s.save': '保存', 's.count': '{n} 个文件已修改'},
               'en': {'s.save': 'Save', 's.count': '{n} file{s} changed'}}
    keys = list(sources['zh'])
    client = _ScriptedClient([
        {'translations': {'s.save': 'Sichern', 's.count': '{n} Datei{s} geändert'}},
        {'translations': {'s.count': 'Geänderte Dateien: {n}'}},
        {'s.save': {'issue': 'Buttons use the standard verb', 'fix': 'Speichern'}},
        {'s.save': 'Speichern'},
    ])
    translator = TOOL.Translator(
        manifest=manifest, keys=keys, sources=sources, derived={}, glossary={'terms': []},
        usage={}, client=client, model='translator', review_model='reviewer', log=lambda _m: None)
    result = translator.run_batch(manifest.language('de'), keys)
    assert result.accepted == {'s.save': 'Speichern', 's.count': 'Geänderte Dateien: {n}'}
    assert result.failed == {}
    assert result.review_changes['s.save']['before'] == 'Sichern'
    assert [model for model, _system, _user in client.requests] == [
        'translator', 'translator', 'reviewer', 'translator']
    assert 'forbidden placeholders' in client.requests[1][2]


def test_review_failure_withholds_unreviewed_translations():
    manifest = _manifest_with('ja')
    sources = {'zh': {'s.save': '保存'}, 'en': {'s.save': 'Save'}}

    class _ReviewDown(_ScriptedClient):
        def complete_json(self, model, system, user):
            if model == 'reviewer':
                raise TOOL.TranslationToolError('reviewer unavailable')
            return super().complete_json(model, system, user)

    client = _ReviewDown([{'translations': {'s.save': '保存'}}])
    translator = TOOL.Translator(
        manifest=manifest, keys=['s.save'], sources=sources, derived={},
        glossary={'terms': []}, usage={}, client=client, model='translator',
        review_model='reviewer', log=lambda _m: None)
    result = translator.run_batch(manifest.language('ja'), ['s.save'])
    assert result.accepted == {}
    assert 'review failed' in result.failed['s.save'][0]


# ── Generator: derived delivery and check gate ───────────────────────────

def _generator_tree(tmp_path: Path, derived_fr: dict) -> Path:
    root = tmp_path / 'repo'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy(GENERATOR_PATH, root / 'scripts' / 'gen_i18n_contract.mjs')
    i18n = root / 'frontend/src/i18n'
    (i18n / 'locales').mkdir(parents=True)
    (i18n / 'generated').mkdir()
    (i18n / 'generated' / 'xx.generated.json').write_text('{}\n', encoding='utf-8')
    manifest = {'default': 'zh', 'fallback': 'en', 'sources': ['zh', 'en'], 'languages': [
        {'code': 'zh', 'tag': 'zh-CN', 'name': '中文', 'englishName': 'Simplified Chinese'},
        {'code': 'en', 'tag': 'en', 'name': 'English', 'englishName': 'English'},
        {'code': 'fr', 'tag': 'fr', 'name': 'Français', 'englishName': 'French'},
    ]}
    (i18n / 'languages.json').write_text(json.dumps(manifest), encoding='utf-8')
    locales = {
        'zh': {'a.count': '{n} 个文件', 'a.save': '保存', 'a.open': '打开'},
        'en': {'a.count': '{n} file{s}', 'a.save': 'Save', 'a.open': 'Open'},
        'fr': derived_fr,
    }
    for code, messages in locales.items():
        (i18n / 'locales' / f'{code}.json').write_text(
            json.dumps(messages, ensure_ascii=False), encoding='utf-8')
    return root


def _generate(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    if shutil.which('node') is None:
        pytest.skip('node is required for the i18n generator')
    return subprocess.run(['node', str(root / 'scripts' / 'gen_i18n_contract.mjs'), mode],
                          capture_output=True, text=True, check=False)


def test_generator_completes_pending_derived_entries_from_the_fallback(tmp_path):
    root = _generator_tree(tmp_path, {
        'a.count': '{n} fichier{s} {s}',  # unusable: repeated placeholder
        'a.save': 'Enregistrer',
        'a.stale': 'Ancienne clé',
    })
    written = _generate(root, '--write')
    assert written.returncode == 0, written.stderr
    assert '1 missing' in written.stderr and '1 unusable' in written.stderr
    generated = root / 'frontend/src/i18n/generated'
    delivered = json.loads((generated / 'fr.generated.json').read_text(encoding='utf-8'))
    assert delivered == {'a.count': '{n} file{s}', 'a.save': 'Enregistrer', 'a.open': 'Open'}
    assert not (generated / 'xx.generated.json').exists()
    languages_module = (root / 'frontend/src/i18n/languages.generated.ts').read_text(encoding='utf-8')
    assert "export type UiLanguage = \"zh\" | \"en\" | \"fr\";" in languages_module
    assert "import frCatalogUrl from './generated/fr.generated.json?url';" in languages_module

    checked = _generate(root, '--check')
    assert checked.returncode == 1
    assert 'fr.json' in checked.stderr and 'translate_i18n_locales.py' in checked.stderr


def test_generator_check_passes_once_derived_locales_are_complete(tmp_path):
    root = _generator_tree(tmp_path, {
        'a.count': 'Fichiers : {n}', 'a.save': 'Enregistrer', 'a.open': 'Ouvrir'})
    assert _generate(root, '--write').returncode == 0
    checked = _generate(root, '--check')
    assert checked.returncode == 0, checked.stderr
    assert '3 keys, 3 languages' in checked.stdout


def test_node_and_python_frame_the_catalog_digest_identically():
    if shutil.which('node') is None:
        pytest.skip('node is required for the i18n generator')
    from lib.vite_assets import _source_i18n_catalog_digest
    node = subprocess.run(
        ['node', '--input-type=module', '-e',
         "import { readI18nCatalog } from './scripts/gen_i18n_contract.mjs';"
         'process.stdout.write((await readI18nCatalog()).digest);'],
        cwd=ROOT, capture_output=True, text=True, check=False)
    assert node.returncode == 0, node.stderr
    assert node.stdout == _source_i18n_catalog_digest()


# ── Server consumers follow the manifest ─────────────────────────────────

def test_python_manifest_consumers_follow_the_frontend_manifest():
    from lib.http_transport import ui_languages
    from lib.conversations import title_gen
    from lib.vite_assets import I18N_LOCALE_PATHS

    codes = tuple(entry['code'] for entry in MANIFEST['languages'])
    assert ui_languages.UI_LANGUAGES == codes
    assert ui_languages.DEFAULT_UI_LANGUAGE == MANIFEST['default']
    assert [Path(path).stem for path in I18N_LOCALE_PATHS] == list(codes)
    for entry in MANIFEST['languages']:
        prompt = title_gen._system_prompt(entry['code'])
        assert f"Write the title in {entry['englishName']}, regardless" in prompt
