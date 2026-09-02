"""Generated, closed-world contracts for frontend product copy."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _node(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ('node', *arguments),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_generated_i18n_contract_and_all_source_references_are_current():
    generated = _node('scripts/gen_i18n_contract.mjs', '--check')
    assert generated.returncode == 0, generated.stderr or generated.stdout
    usage = _node('scripts/check_i18n_usage.mjs')
    assert usage.returncode == 0, usage.stderr or usage.stdout
    assert 'TS/JS/HTML closed world' in usage.stdout


def test_i18n_usage_checker_has_a_missing_key_negative_control():
    probe = """
      import { inspectScript } from './scripts/check_i18n_usage.mjs';
      const problems = [];
      inspectScript(
        '/synthetic/i18n-probe.ts',
        "translate('contract.__missing_probe__')",
        new Set(['contract.known']),
        problems,
      );
      process.stdout.write(JSON.stringify(problems));
    """
    result = _node('--input-type=module', '--eval', probe)
    assert result.returncode == 0, result.stderr
    assert 'undefined key contract.__missing_probe__' in result.stdout


def test_i18n_usage_checker_has_a_placeholder_negative_control():
    probe = """
      import { inspectScript } from './scripts/check_i18n_usage.mjs';
      const problems = [];
      inspectScript(
        '/synthetic/i18n-probe.ts',
        "t('contract.count', { wrong: 1 })",
        new Set(['contract.count']),
        problems,
        new Map([['contract.count', ['count']]]),
      );
      process.stdout.write(JSON.stringify(problems));
    """
    result = _node('--input-type=module', '--eval', probe)
    assert result.returncode == 0, result.stderr
    assert 'missing=[\\"count\\"] extra=[\\"wrong\\"]' in result.stdout


def test_public_translator_is_generated_key_typed_and_dom_adapter_is_internal():
    source = (ROOT / 'frontend/src/i18n/index.ts').read_text(encoding='utf-8')
    generated = (
        ROOT / 'frontend/src/i18n/contract.generated.ts'
    ).read_text(encoding='utf-8')
    assert 'export const t: Translator' in source
    assert 'export function t(key: string' not in source
    assert 'translateMessage(key)' in source
    assert 'export type I18nKey =' in generated
    assert 'export interface I18nParamsByKey {' in generated
    assert '? readonly [params?: never]' in generated
    assert ': readonly [params: I18nParamsFor<K>]' in generated
