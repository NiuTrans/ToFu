"""tests/test_frontend_timerblock_i18n.py — dedicated hard gate for the
Timer-Watcher block's ``timerBlock.*`` i18n namespace.

WHY
---
The whole ``timerBlock.*`` namespace shipped to production rendering RAW KEYS
(owner saw literal ``timerBlock.headDone`` / ``timerBlock.verifying`` in the
collapsed timer card, 2026-07-16). The render code used the
``_t(key, fallback)`` wrapper which looks safe, but ``i18n.js``'s ``t()``
returns the KEY STRING when a key is missing — its 2nd arg is ``params``, NOT a
fallback — so the wrapper's fallback is DEAD CODE and the raw key leaked.

The locale catalogs are now generated under ``frontend/src/i18n`` (typed
``translateMessage`` + generated JSON), not the retired ``static/js/i18n.js``.
This file gives the timer namespace its OWN isolated green/red signal so a
fresh ``timerBlock`` leak cannot be masked by a pre-existing sibling-namespace
failure.

It also adds a standing Node harness that drives the REAL shipped
``translateMessage`` substitution semantics over the REAL generated catalogs
through the exact header / cadence ``.replace()`` chains from
``tool_rounds.js`` and asserts placeholders actually SUBSTITUTE (``{status}`` →
``completed``), in both zh and en — proving the keys aren't merely *present*
but actually *render*. A NEUTER (delete one key on a mutated copy; shipped file
untouched) proves the substitution assertion is load-bearing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root, shipped_source_text

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, '..'))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
I18N_EN = os.path.join(
    REPO_ROOT, 'frontend', 'src', 'i18n', 'generated', 'en.generated.json')
I18N_ZH = os.path.join(
    REPO_ROOT, 'frontend', 'src', 'i18n', 'generated', 'zh.generated.json')

NAMESPACE = 'timerBlock.'

# A literal key reference, capturing the char after the closing quote so we can
# skip dynamic concatenation prefixes (none exist today, but stay future-proof).
_KEY_REF_RE = re.compile(r"""['"`](timerBlock\.[A-Za-z0-9_.]*)['"`]\s*(.?)""")


def _is_dynamic_prefix(key: str, next_char: str) -> bool:
    return key.endswith('.') or next_char == '+'


def _referenced_keys() -> set[str]:
    """Complete literal timerBlock.* keys referenced across source JS."""
    out: set[str] = set()
    for root, dirs, files in os.walk(JS_DIR):
        dirs[:] = [d for d in dirs if not d.startswith(('.', '__'))]
        for name in sorted(files):
            if not name.endswith('.js') or name == 'i18n.js':
                continue
            if name.startswith('bundle-'):
                continue
            try:
                with open(os.path.join(root, name), 'r', encoding='utf-8') as f:
                    text = f.read()
            except OSError:
                continue
            for m in _KEY_REF_RE.finditer(text):
                key, nxt = m.group(1), m.group(2)
                if key == NAMESPACE or _is_dynamic_prefix(key, nxt):
                    continue
                out.add(key)
    return out


def _catalog_keys(path: str) -> set[str]:
    with open(path, 'r', encoding='utf-8') as f:
        return set(json.load(f).keys())


def _defined_keys() -> set[str]:
    return _catalog_keys(I18N_EN)


# ── The hard gate ─────────────────────────────────────────────────────
def test_every_referenced_timerblock_key_is_defined():
    """Every complete literal timerBlock.* key used in source JS MUST be
    defined in the en catalog — else t() renders the raw key in the timer
    card."""
    defined = _defined_keys()
    missing = sorted(k for k in _referenced_keys() if k not in defined)
    assert not missing, (
        'Source references timerBlock.* i18n keys NOT defined in '
        'frontend/src/i18n/generated/en.generated.json — t() would render '
        'the raw key in the timer watcher card. Add each (zh + en):\n'
        + '\n'.join(f'  {k}' for k in missing)
    )


def test_timerblock_keys_have_both_zh_and_en():
    """Each timerBlock.* entry must define BOTH zh and en (a zh-only entry
    silently falls back to zh for English users)."""
    en = set(json.loads(
        shipped_source_text('frontend/src/i18n/generated/en.generated.json')).keys())
    zh = set(json.loads(
        shipped_source_text('frontend/src/i18n/generated/zh.generated.json')).keys())
    en_only = sorted(k for k in en if k.startswith(NAMESPACE) and k not in zh)
    zh_only = sorted(k for k in zh if k.startswith(NAMESPACE) and k not in en)
    assert not en_only, 'timerBlock.* entries missing zh: ' + ', '.join(en_only)
    assert not zh_only, 'timerBlock.* entries missing en: ' + ', '.join(zh_only)


def test_scanner_sees_known_timerblock_keys():
    """Sanity floor: the scanner must actually find live timer keys, proving
    it isn't vacuously matching nothing (which would make the gate green for
    the wrong reason)."""
    refs = _referenced_keys()
    for key in ('timerBlock.headDone', 'timerBlock.headWatching', 'timerBlock.cadence'):
        assert key in refs, (
            f'{key} not discovered by the scanner — the extraction regex is '
            'too aggressive or tool_rounds.js changed.'
        )


def test_gate_would_catch_an_undefined_newcomer():
    """Negative control: a synthetic undefined key is extracted and would be
    flagged — proving the gate is load-bearing, not perpetually green."""
    fake = 'timerBlock.__nonexistent_probe__'
    defined = _defined_keys()
    assert fake not in defined
    extracted = {m.group(1) for m in _KEY_REF_RE.finditer(f"_t('{fake}', 'x');")}
    assert fake in extracted, 'scanner failed to extract the synthetic key'


# ── Node harness: prove placeholders actually SUBSTITUTE (zh + en) ─────
def _node_available() -> bool:
    return bool(shutil.which('node'))


# Loads the REAL generated locale catalogs under bare node, mirrors the typed
# translateMessage() from frontend/src/i18n/index.ts, then replays the exact
# header/cadence .replace() chains from tool_rounds.js and asserts substitution
# + no raw keys.
_HARNESS = r"""
const fs = require('fs');
const EN = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const ZH = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const loaded = { en: EN, zh: ZH };
let _i18nLang = 'en';

// Byte-mirror of frontend/src/i18n/index.ts translateMessage().
function translateMessage(key, params) {
  const primary = loaded[_i18nLang];
  const fallback = loaded['zh'];
  let value = primary?.[key] ?? fallback?.[key];
  if (value === undefined) {
    value = key;
  }
  if (!params) return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name] ?? '') : token
  ));
}
const t = (key, params) => translateMessage(key, params);
const setLang = (lang) => { _i18nLang = lang; };

const out = [];
const check = (n, c) => out.push((c ? 'PASS ' : 'FAIL ') + n);

const _s = (n) => (n !== 1 ? 's' : '');
// headDone chain — tool_rounds.js
const headDone = (status, n) => t('timerBlock.headDone')
  .replace('{id}','tmr_615f1266').replace('{status}', status)
  .replace('{n}', n).replace('{s}', _s(n));
// headWatching chain (has {skip}{err})
const headWatching = (n, skip, err) => {
  const skipSuffix = skip > 0 ? t('timerBlock.headSkipSuffix').replace('{n}', skip) : '';
  const errSuffix = err ? t('timerBlock.headErrSuffix') : '';
  return t('timerBlock.headWatching')
    .replace('{id}','tmr_615f1266').replace('{n}', n).replace('{s}', _s(n))
    .replace('{skip}', skipSuffix).replace('{err}', errSuffix);
};
// cadence chain
const cadence = (interval, maxPolls) => maxPolls > 0
  ? t('timerBlock.cadenceMax').replace('{n}', interval).replace('{m}', maxPolls)
  : t('timerBlock.cadence').replace('{n}', interval);

function assertLang(lang) {
  setLang(lang);
  const hd = headDone('completed', 40);
  const hd1 = headDone('completed', 1);
  const hw = headWatching(40, 3, true);
  const cm = cadence(60, 120);
  // The exact leak the owner saw: header must NOT be the raw key, and
  // {status} must be filled with the real status text.
  check(lang + '_headDone_not_raw_key', hd.indexOf('timerBlock.') < 0);
  check(lang + '_headDone_status_filled', hd.indexOf('completed') >= 0 && hd.indexOf('{status}') < 0);
  check(lang + '_headDone_n_filled', hd.indexOf('40') >= 0 && hd.indexOf('{n}') < 0);
  check(lang + '_headWatching_filled', hw.indexOf('{') < 0 && hw.indexOf('timerBlock.') < 0);
  check(lang + '_cadenceMax_filled', cm.indexOf('60') >= 0 && cm.indexOf('120') >= 0 && cm.indexOf('{') < 0);
  // No stray placeholder braces anywhere in the assembled strings.
  const all = [hd, hd1, hw, cm].join(' ');
  check(lang + '_no_leftover_braces', !/\{(id|status|n|s|m|skip|err)\}/.test(all));
  out.push('SAMPLE ' + lang + ' | ' + hd + ' | ' + cm);
}
assertLang('en');
assertLang('zh');

// zh must differ from en for a translated key (proves zh really localized,
// not just echoing the English via the zh||key fallback path).
setLang('en'); const hdEn = headDone('completed', 40);
setLang('zh'); const hdZh = headDone('completed', 40);
check('zh_differs_from_en', hdEn !== hdZh && hdZh.indexOf('次检查') >= 0);

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_timerblock_placeholders_substitute_live():
    """Drive the REAL generated catalogs through the timer header/cadence
    replace chains and assert placeholders fill (zh + en), not just that keys
    exist."""
    harness = os.path.join(HERE, '_timerblock_i18n_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, I18N_EN, I18N_ZH],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    for ln in output.splitlines():
        if ln.startswith('SAMPLE '):
            print('  ' + ln)
    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'timerBlock substitution failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_missing_key_leaks_raw_key():
    """NEUTER (load-bearing proof): delete timerBlock.headDone on a MUTATED
    COPY of the en catalog (shipped file untouched) → the header substitution
    assertion must FAIL because t() falls back to the raw key. Proves the
    live-substitution test above actually catches a missing key."""
    en_catalog = json.loads(
        shipped_source_text('frontend/src/i18n/generated/en.generated.json'))
    zh_catalog = json.loads(
        shipped_source_text('frontend/src/i18n/generated/zh.generated.json'))
    assert 'timerBlock.headDone' in en_catalog, 'headDone key missing — test drift'
    neutered_en = {k: v for k, v in en_catalog.items() if k != 'timerBlock.headDone'}
    neutered_zh = {k: v for k, v in zh_catalog.items() if k != 'timerBlock.headDone'}
    assert 'timerBlock.headDone' not in neutered_en
    assert 'timerBlock.headDone' not in neutered_zh

    tmp_en = os.path.join(HERE, '_i18n_neutered_en.json')
    tmp_zh = os.path.join(HERE, '_i18n_neutered_zh.json')
    harness = os.path.join(HERE, '_timerblock_neuter_harness.js')
    with open(tmp_en, 'w', encoding='utf-8') as f:
        json.dump(neutered_en, f)
    with open(tmp_zh, 'w', encoding='utf-8') as f:
        json.dump(neutered_zh, f)
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, tmp_en, tmp_zh],
            capture_output=True, text=True, timeout=60,
        )
        output = proc.stdout.strip()
        assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
        # With headDone gone, the "not raw key" / "status filled" checks FAIL.
        assert 'FAIL en_headDone_not_raw_key' in output or \
               'FAIL en_headDone_status_filled' in output, (
            'NEUTER did not surface the leak — the substitution assertions '
            'are NOT load-bearing:\n' + output
        )
    finally:
        for p in (tmp_en, tmp_zh, harness):
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
