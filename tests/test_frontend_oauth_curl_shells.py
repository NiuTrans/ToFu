"""tests/test_frontend_oauth_curl_shells.py — the OAuth curl helper must emit a
runnable command for the shell the user will actually paste into.

Defect: ``_buildCurlCommand`` (static/js/settings/oauth.js) rendered ONLY the
bash form — single quotes + ``\\`` line continuations. Pasted into Windows CMD
the URL parses as ``'https:`` (curl: (3) "Port number was not a decimal
number") and each continuation line becomes an unknown command — the exact
failure the owner hit on 2026-07-31.

Fix shape: ONE parameter assembly, three shell renderers (bash / powershell /
cmd), a selector in the helper UI. Sniffing the browser platform is NOT the
fix — this helper exists for the case where the paste target is routinely a
DIFFERENT machine than the browser (self-hosted server + local terminal, VS
Code tunnel), so all three variants are offered and the sniff only picks which
shows first.

Evidence levels (honest about what this host cannot prove):
  • bash — REAL EXECUTION: the rendered script runs under bash with a stubbed
    curl and the argv must round-trip byte-exactly, including an adversarial
    payload (apostrophe / backslash / double-quote / dollar / backtick).
  • cmd — MSVCRT simulation: the rendered line is parsed by a Python
    re-implementation of CommandLineToArgvW (the rules curl.exe itself uses);
    real CMD cannot run on this host.
  • powershell — structural round-trip: continuation joins + single-quote
    literal parsing; pwsh is not available on this host.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_oauth_curl_shells.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
OAUTH_JS = os.path.join(ROOT, 'static', 'js', 'settings', 'oauth.js')
SETTINGS_CSS = os.path.join(ROOT, 'static', 'settings.css')

NODE = shutil.which('node')
BASH = shutil.which('bash')

EXCHANGE = {
    'token_url': 'https://token.example.test/v1/oauth/token',
    'redirect_uri': 'https://token.example.test/oauth/code/callback',
    'client_id': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
    'code_verifier': 'e88RG5kYpUCgoTiiLZEIHrYjXKIxW_WyL8FZX-vsju4bbYJIGk',
    'state': '8e241785e77649f191e509836ec2d40b',
}
EXCHANGE_FORM = dict(EXCHANGE, style='form')

# Realistic payloads plus one adversarial one. The adversarial code carries
# every character class that breaks a naive quoting scheme.
ADV_CODE = "c'o\\de$`tick\" end"

_HARNESS_JS = r"""
const fs = require('fs');
globalThis.window = globalThis;
globalThis.addEventListener = function(){};
// node 21+ HAS a global navigator whose userAgent is getter-only — plain
// assignment silently no-ops and the UA sniff would read 'Node.js/…'.
function setUA(ua) {
  Object.defineProperty(globalThis, 'navigator',
    { value: { userAgent: ua }, configurable: true, writable: true });
}
setUA('');
// node HAS a global BroadcastChannel (worker_threads): oauth.js would open
// one at load and the channel would keep the event loop alive FOREVER — the
// harness would never exit. The browser fallback path is what we test.
delete globalThis.BroadcastChannel;
eval(fs.readFileSync(process.argv[1], 'utf8'));
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
_oauthExchangeParams['claude'] = cases.exJson;
_oauthExchangeParams['codex']  = cases.exForm;
const out = {};
for (const sh of ['bash', 'powershell', 'cmd', 'zsh']) {
  out['json:' + sh] = _buildCurlCommand('claude', cases.code, cases.state, sh);
  out['form:' + sh] = _buildCurlCommand('codex',  cases.code, cases.state, sh);
}
setUA('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36');
out['ua:win'] = _curlDefaultShell();
setUA('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15');
out['ua:mac'] = _curlDefaultShell();
setUA('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36');
out['ua:linux'] = _curlDefaultShell();
out['shells'] = _CURL_SHELLS;
process.stdout.write(JSON.stringify(out), () => process.exit(0));
"""


def _render_all(src: str | None = None, code: str = 'CODE', state: str = 'STATE') -> dict:
    """Execute the REAL oauth.js under node and return the rendered commands."""
    if NODE is None:
        pytest.skip('node is required to execute oauth.js')
    path = OAUTH_JS
    tmp = None
    if src is not None:
        tmp = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        tmp.write(src)
        tmp.close()
        path = tmp.name
    try:
        cases = {'exJson': EXCHANGE, 'exForm': EXCHANGE_FORM, 'code': code, 'state': state}
        proc = subprocess.run([NODE, '-e', _HARNESS_JS, path], input=json.dumps(cases),
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, f'harness failed: {proc.stderr[:800]}'
        return json.loads(proc.stdout)
    finally:
        if tmp is not None:
            os.unlink(tmp.name)


def _json_body(code: str, state: str) -> str:
    """The exact bytes JSON.stringify produces for the JSON exchange body."""
    obj = {
        'grant_type': 'authorization_code',
        'code': code,
        'state': state or EXCHANGE['state'],
        'redirect_uri': EXCHANGE['redirect_uri'],
        'client_id': EXCHANGE['client_id'],
        'code_verifier': EXCHANGE['code_verifier'],
    }
    return json.dumps(obj, separators=(',', ':'), ensure_ascii=False)


def _form_body(code: str) -> str:
    return urllib.parse.urlencode({
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': EXCHANGE['redirect_uri'],
        'client_id': EXCHANGE['client_id'],
        'code_verifier': EXCHANGE['code_verifier'],
    })


def _msvcrt_split(cmdline: str) -> list[str]:
    """CommandLineToArgvW / MSVCRT argument parsing — the rules curl.exe uses.

    Backslashes are literal EXCEPT before a quote: 2n backslashes + quote →
    n backslashes + quote-toggle; 2n+1 backslashes + quote → n backslashes +
    a literal quote. Outside quotes, space separates.
    """
    args, cur = [], []
    in_quotes = False
    i, n = 0, len(cmdline)
    while i < n:
        ch = cmdline[i]
        if ch == '\\':
            j = i
            while j < n and cmdline[j] == '\\':
                j += 1
            run = j - i
            if j < n and cmdline[j] == '"':
                cur.append('\\' * (run // 2))
                if run % 2 == 0:
                    in_quotes = not in_quotes
                else:
                    cur.append('"')
                i = j + 1
            else:
                cur.append('\\' * run)
                i = j
        elif ch == '"':
            in_quotes = not in_quotes
            i += 1
        elif ch in ' \t' and not in_quotes:
            if cur:
                args.append(''.join(cur))
                cur = []
            while i < n and cmdline[i] in ' \t':
                i += 1
        else:
            cur.append(ch)
            i += 1
    if cur:
        args.append(''.join(cur))
    return args


def _ps_split(cmdline: str) -> list[str]:
    """PowerShell tokenizing for the subset we emit: bare words + single-quoted
    literals (no interpolation), ``''`` → literal quote, backtick-newline is a
    continuation."""
    logical = cmdline.replace('`\n', ' ').replace('`\r\n', ' ')
    tokens, cur = [], []
    i, n = 0, len(logical)
    while i < n:
        ch = logical[i]
        if ch in ' \t':
            if cur:
                tokens.append(''.join(cur))
                cur = []
            i += 1
        elif ch == "'":
            i += 1
            while i < n:
                if logical[i] == "'":
                    if i + 1 < n and logical[i + 1] == "'":
                        cur.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    cur.append(logical[i])
                    i += 1
        else:
            cur.append(ch)
            i += 1
    if cur:
        tokens.append(''.join(cur))
    return tokens


# ── ① bash: byte-parity with the legacy render (regression anchor) ─────────

def test_bash_variant_byte_identical_to_legacy_render():
    out = _render_all()
    body = _json_body('CODE', 'STATE')
    legacy = ("curl '" + EXCHANGE['token_url'] + "' \\\n"
              "  -H 'Content-Type: application/json' \\\n"
              "  --data-raw '" + body + "'")
    assert out['json:bash'] == legacy
    legacy_form = ("curl '" + EXCHANGE['token_url'] + "' \\\n"
                   "  -H 'Content-Type: application/x-www-form-urlencoded' \\\n"
                   "  --data-raw '" + _form_body('CODE') + "'")
    assert out['form:bash'] == legacy_form


# ── ② bash: REAL execution — argv must round-trip byte-exactly ─────────────

@pytest.mark.skipif(BASH is None, reason='bash not available on this host')
def test_bash_variant_really_executes_with_adversarial_payload():
    out = _render_all(code=ADV_CODE)
    expected_body = _json_body(ADV_CODE, 'STATE')
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, 'curl')
        with open(stub, 'w', encoding='utf-8') as f:
            f.write('#!/bin/bash\n'
                    'printf "%s" "$#" > "$CURL_STUB_DIR/argc"\n'
                    'i=0\nfor a in "$@"; do printf "%s" "$a" > "$CURL_STUB_DIR/arg_$i"; i=$((i+1)); done\n')
        os.chmod(stub, 0o755)
        script = os.path.join(d, 'cmd.sh')
        with open(script, 'w', encoding='utf-8') as f:
            f.write(out['json:bash'] + '\n')
        env = dict(os.environ, PATH=d + os.pathsep + os.environ.get('PATH', ''),
                   CURL_STUB_DIR=d)
        proc = subprocess.run([BASH, script], env=env, capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f'rendered bash failed to parse/run: {proc.stderr[:400]}'
        with open(os.path.join(d, 'argc'), encoding='utf-8') as f:
            assert f.read() == '5'
        argv = []
        for i in range(5):
            with open(os.path.join(d, f'arg_{i}'), encoding='utf-8') as f:
                argv.append(f.read())
        assert argv == [EXCHANGE['token_url'], '-H', 'Content-Type: application/json',
                        '--data-raw', expected_body], (
            'bash did not deliver the payload byte-exactly — quoting is broken')


# ── ③ powershell: shape + single-quote round-trip ──────────────────────────

def test_powershell_variant_shape_and_roundtrip():
    out = _render_all(code=ADV_CODE)
    cmd = out['json:powershell']
    assert cmd.startswith('curl.exe '), (
        "PowerShell aliases `curl` to Invoke-WebRequest, which rejects -H/--data-raw "
        '— the binary must be named curl.exe')
    tokens = _ps_split(cmd)
    assert tokens == ['curl.exe', EXCHANGE['token_url'], '-H',
                      'Content-Type: application/json', '--data-raw',
                      _json_body(ADV_CODE, 'STATE')], (
        'PowerShell single-quoting does not round-trip the payload')
    form_tokens = _ps_split(out['form:powershell'])
    assert form_tokens[-1] == _form_body(ADV_CODE)
    assert form_tokens[0] == 'curl.exe'


# ── ④ cmd: single line + MSVCRT round-trip ─────────────────────────────────

def test_cmd_variant_single_line_and_msvcrt_roundtrip():
    out = _render_all(code=ADV_CODE)
    cmd = out['json:cmd']
    assert '\n' not in cmd, 'CMD has no line continuation — must be one line'
    assert not cmd.startswith('curl.exe'), 'CMD resolves curl.exe via bare `curl`'
    argv = _msvcrt_split(cmd)
    assert argv == ['curl', EXCHANGE['token_url'], '-H',
                    'Content-Type: application/json', '--data-raw',
                    _json_body(ADV_CODE, 'STATE')], (
        'CMD quoting does not survive CommandLineToArgvW — '
        'backslashes must double BEFORE quotes are escaped')
    form_argv = _msvcrt_split(out['form:cmd'])
    assert form_argv[-1] == _form_body(ADV_CODE)


# ── ⑤ default-shell sniff + unknown-shell fallback ─────────────────────────

def test_sniff_only_picks_the_default_and_unknown_shells_fall_back():
    out = _render_all()
    assert out['ua:win'] == 'powershell'
    assert out['ua:mac'] == 'bash'
    assert out['ua:linux'] == 'bash'
    # An unrecognized shell name must degrade to the sniffed default ('' UA → bash)
    assert out['json:zsh'] == out['json:bash']
    # All three real variants are DISTINCT — otherwise the selector is decorative.
    assert out['json:bash'] != out['json:powershell'] != out['json:cmd'] != out['json:bash']
    assert out['shells'] == ['bash', 'powershell', 'cmd']


# ── ⑥ helper UI: every variant offered, switch re-targets the copy buffer ──

def test_helper_ui_offers_all_shells_and_switch_retargets_copy():
    src = open(OAUTH_JS, encoding='utf-8').read()
    assert '_CURL_SHELLS.map(' in src, 'no tab bar rendered from _CURL_SHELLS'
    for label in ('bash / zsh', 'PowerShell', 'CMD'):
        assert label in src, f'shell label {label!r} missing'
    switch_pos = src.find('btn.onclick = function() {',
                          src.find("querySelectorAll('.oauth-curl-shell')"))
    assert switch_pos != -1, 'shell tabs have no switch handler'
    assert '_buildCurlCommand(provider, code, state, s)' in src[switch_pos:switch_pos + 400], (
        'the switch handler must re-render with the CHOSEN shell, not the default')
    assert 'ta.value = _buildCurlCommand(provider, code, state, s)' in src[switch_pos:switch_pos + 400], (
        'the switch must write into the SAME textarea the copy button reads')
    assert '_safeClipboardWrite(ta.value)' in src, (
        'copy must read the textarea AFTER any switch, not a pre-rendered capture')


def test_active_shell_tab_style_exists_and_follows_the_plain_rule():
    css = open(SETTINGS_CSS, encoding='utf-8').read()
    plain_pos = css.find('.oauth-manual-fallback .btn-small:not(.btn-primary):not(.btn-danger)')
    active_pos = css.find('.oauth-manual-fallback .btn-small.oauth-curl-shell.active')
    assert plain_pos != -1 and active_pos != -1, 'active-tab rule missing'
    assert active_pos > plain_pos, (
        'equal specificity (0,4,0) — the active rule must come AFTER the plain '
        'button rule or the active tab renders white-on-white')


# ── NEUTERs: prove the byte-revert defeats the guards ──────────────────────

def test_NEUTER_shell_plumbing_killed_collapses_all_variants_to_bash():
    """Kill the shell dispatch (`var use = 'bash';`) — every variant must then
    render bash, i.e. the discriminating assertion ⑤ relies on is defeated."""
    src = open(OAUTH_JS, encoding='utf-8').read()
    needle = 'var use = _CURL_SHELLS.indexOf(shell) >= 0 ? shell : _curlDefaultShell();'
    assert needle in src, 'NEUTER anchor missing'
    neutered = src.replace(needle, "var use = 'bash';", 1)
    out = _render_all(src=neutered)
    assert out['json:powershell'] == out['json:bash'] == out['json:cmd'], (
        'with dispatch killed the variants must COLLAPSE — proving the real '
        'outputs genuinely differ per shell')


def test_NEUTER_switch_without_shell_argument_defeats_ui_guard():
    src = open(OAUTH_JS, encoding='utf-8').read()
    neutered = src.replace('_buildCurlCommand(provider, code, state, s)',
                           '_buildCurlCommand(provider, code, state)', 1)
    assert neutered != src, 'NEUTER anchor missing'
    assert '_buildCurlCommand(provider, code, state, s)' not in neutered, (
        'after the revert the guard needle must be gone')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
