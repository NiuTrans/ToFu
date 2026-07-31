"""Frontend guard: a QR recovered from terminal output must actually RENDER
inside the run_command block, and must not perturb the block when absent.

Renders ``_renderUnifiedToolLine`` (static/js/ui/tool_rounds.js) through node
with the same minimal global surface the wire-parity harness uses.

The invariant that matters: a scan-to-login code must be visible WITHOUT the
user expanding anything. The output ``<pre>`` is collapsed by default AND is
``white-space: pre-wrap`` + ``word-break: break-all`` — the very thing that
destroys block-art QR rows — so the image has to sit outside it. A test that
merely asserted "the html contains the uri" would pass even if the img were
nested inside the collapsed pane, so the position is asserted explicitly.

Run: pytest tests/test_frontend_qr_render.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOL_ROUNDS = ROOT / 'static' / 'js' / 'ui' / 'tool_rounds.js'
I18N = ROOT / 'static' / 'js' / 'i18n.js'

_URI = 'data:image/png;base64,QQQQ'


def _i18n_dict() -> dict:
    """Scrape ``static/js/i18n.js`` into ``{key: {zh, en}}``.

    Parsing the REAL file (instead of hand-listing keys in the test) is what
    makes the harness honest: a key absent from production is absent here too,
    so ``t()`` returns the bare key in BOTH places and the guard can see it.
    """
    import re
    src = I18N.read_text(encoding='utf-8')
    pat = re.compile(
        r"""^[ \t]*'([\w.\-]+)':\s*\{\s*zh:\s*(['"])(.*?)\2\s*,"""
        r"""\s*en:\s*(['"])(.*?)\4""",
        re.MULTILINE)
    return {m.group(1): {'zh': m.group(3), 'en': m.group(5)}
            for m in pat.finditer(src)}


def _render(round_obj: dict) -> str:
    if shutil.which('node') is None:
        pytest.skip('node is required for the QR render guard')
    harness = textwrap.dedent('''
        const fs = require('fs');
        global.escapeHtml = (s) => String(s == null ? '' : s)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        // ★ t() is driven by the REAL dictionary (injected as argv[3] JSON),
        // not a fallback-inventing fake.
        //
        // Production signature is t(key, params), where params is a
        // {placeholder} substitution map — NOT a default string. A harness
        // written as `(k, d) => (d || k)` INVERTS that: it manufactures prose
        // the real UI never produces, so a MISSING key looks fine in tests and
        // ships to users as the raw key ("project.qrScan"). That exact blind
        // spot let two undefined keys reach production while 11 render guards
        // stayed green. Here a missing key returns the key, as production does.
        const _dict = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
        global.t = (key, params) => {
          const e = _dict[key];
          let text = e && e.en != null ? e.en : key;   // missing → the KEY
          if (params) {
            for (const k in params) {
              if (Object.prototype.hasOwnProperty.call(params, k)) {
                text = text.split('{' + k + '}').join(params[k]);
              }
            }
          }
          return text;
        };
        global.Icon = (n, s) => '<ICON>';
        global.renderMarkdown = (s) => s;
        global._shortUrl = (u) => u;
        global.formatNumber = (n) => String(n);
        global.window = { location: { href: 'http://localhost/' },
                          addEventListener() {}, removeEventListener() {} };
        global.document = { addEventListener() {}, removeEventListener() {},
          createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }) };
        // NOTE: with `node -e`, args after `--` start at argv[1] (there is no
        // script-path entry), unlike a script file where they start at argv[2].
        eval(fs.readFileSync(process.argv[1], 'utf8'));
        const round = JSON.parse(process.argv[2]);
        // isSearching mirrors the real dispatcher: an in-flight round routes to
        // the running-state renderer, a settled one to the done renderer.
        const isSearching = round.status === 'searching';
        process.stdout.write(_renderUnifiedToolLine(round, isSearching));
        process.exit(0);
    ''')
    import tempfile
    with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as fh:
        json.dump(_i18n_dict(), fh)
        dict_path = fh.name
    try:
        proc = subprocess.run(
            ['node', '-e', harness, '--', str(TOOL_ROUNDS),
             json.dumps(round_obj), dict_path],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.unlink(dict_path)
    if proc.returncode != 0:
        raise AssertionError(f'render harness failed:\n{proc.stderr[:2000]}')
    return proc.stdout


def _cmd_round(**meta_extra) -> dict:
    meta = {'toolName': 'run_command', 'command': 'gh auth login',
            'output': 'Scan the QR below\n<blockart>', 'exitCode': '0'}
    meta.update(meta_extra)
    return {'roundNum': 4, 'toolName': 'run_command', 'status': 'done',
            'query': 'gh auth login', 'results': [meta]}


def _running_round(**round_extra) -> dict:
    """A run_command STILL IN FLIGHT — the state a scan-to-login command sits
    in while it blocks waiting for the scan. Note the descriptors live on the
    ROUND here: tool_progress delivers them before any `results` exists."""
    r = {'roundNum': 4, 'toolName': 'run_command', 'status': 'searching',
         'query': 'gh auth login',
         '_partialOutput': 'Open this QR:\n<blockart>\nWaiting for scan...',
         'results': []}
    r.update(round_extra)
    return r


class TestQrLabelIsTranslated:
    """The label above the code must be PROSE, never a raw i18n key.

    This class exists because the first shipped version rendered the literal
    string ``project.qrScan`` to users. Two mistakes compounded:

    * ``t()``'s second argument is a ``{placeholder}`` params map, NOT a
      default string, so ``t('project.qrScan', 'Scannable QR code')`` supplied
      no fallback — it silently did nothing, and the key was never defined.
    * The harness faked ``t`` as ``(k, d) => (d || k)``, INVERTING that
      semantic and manufacturing prose the real UI never produced. Eleven
      render guards passed against a function that does not exist.

    The harness now reads the real dictionary, so these fail when the keys go
    missing — which is exactly the state that shipped.
    """

    def test_single_code_label_is_real_prose(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'project.qrScan' not in html, (
            'the raw i18n key leaked into the UI — the key is undefined in '
            'static/js/i18n.js (t() takes params, not a fallback string)'
        )
        assert 'Scannable QR code' in html

    def test_multi_code_label_is_real_prose(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'},
            {'uri': 'data:image/png;base64,WWWW', 'format': 'png',
             'filename': 'qr-2.png'}]))
        assert 'project.qrScanMulti' not in html
        assert 'scannable QR codes' in html

    def test_label_never_looks_like_an_undefined_key(self):
        """Generic net for FUTURE labels added to this component: the rendered
        label must never be a bare ``namespace.camelCase`` token."""
        import re
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        m = re.search(r'ptool-qr-label">([^<]*)<', html)
        assert m, 'label element missing'
        text = m.group(1).strip()
        assert not re.fullmatch(r'\d*\s*\w+\.[a-z][A-Za-z0-9]*', text), (
            f'label looks like an undefined i18n key: {text!r}')

    def test_harness_really_loaded_the_dictionary(self):
        """Guard the guard: if the scrape returned nothing, every key would
        look missing and the assertions above would compare key-against-key.
        A non-trivial dictionary with our keys present proves it parsed."""
        d = _i18n_dict()
        assert len(d) > 500, f'dictionary scrape looks broken: {len(d)} keys'
        assert d.get('project.qrScan', {}).get('zh')
        assert d.get('project.qrScanMulti', {}).get('en')


class TestQrRendersWhileCommandStillRunning:
    """The acceptance criterion for scan-to-login: the code is visible DURING
    the wait. A QR that only appears when the command exits is useless — the
    authorization window has closed by then."""

    def test_running_command_renders_its_qr(self):
        html = _render(_running_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-cmd-running' in html, 'not the running-state block'
        assert 'ptool-qr-strip' in html, (
            'a still-running command did not render its QR — the user cannot '
            'scan it until the command exits, which is too late'
        )
        assert f'src="{_URI}"' in html

    def test_running_qr_precedes_and_stays_out_of_the_live_pane(self):
        """The live pane is `pre-wrap` + `word-break: break-all` — the exact
        styling that destroys block-art QR rows. The image must sit before it,
        not inside it."""
        html = _render(_running_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        qr_at = html.index('ptool-qr-strip')
        live_at = html.index('ptool-cmd-output-live')
        assert qr_at < live_at
        assert 'ptool-qr' not in html[live_at:]

    def test_running_without_qr_is_unchanged(self):
        html = _render(_running_round())
        assert 'ptool-cmd-running' in html
        assert 'ptool-qr' not in html

    def test_qr_shows_before_any_output_has_streamed(self):
        """A CLI can print the QR as its very first bytes; the strip must not
        depend on the live-output pane existing."""
        html = _render(_running_round(_partialOutput='', qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert 'ptool-cmd-output-live' not in html


class TestQrRendersInTerminalBlock:
    def test_qr_image_is_emitted(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert f'src="{_URI}"' in html
        assert '<img' in html

    def test_qr_sits_outside_the_collapsed_output_pane(self):
        """The whole point: the code must be scannable without clicking
        "Show output". ``.ptool-cmd-output`` is ``display:none`` until
        expanded (and re-wraps text), so an img inside it is useless."""
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        qr_at = html.index('ptool-qr-strip')
        out_at = html.index('ptool-cmd-output-wrap')
        assert qr_at < out_at, 'QR must precede the collapsible output wrapper'
        tail = html[html.index('ptool-cmd-output-wrap'):]
        assert 'ptool-qr' not in tail, 'QR must not be nested in the output pane'

    def test_no_qr_field_leaves_no_qr_markup(self):
        html = _render(_cmd_round())
        assert 'ptool-qr' not in html
        assert 'ptool-cmd-block' in html  # the block itself still renders

    def test_descriptor_without_uri_is_skipped(self):
        """The IndexedDB cache strips multi-MB ``uri`` fields on write; a
        descriptor that lost its uri must degrade to "no image", never to a
        broken <img src="">."""
        html = _render(_cmd_round(qrImages=[{'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr' not in html
        assert 'src=""' not in html

    def test_multiple_codes_all_render(self):
        html = _render(_cmd_round(qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'},
            {'uri': 'data:image/png;base64,WWWW', 'format': 'png',
             'filename': 'qr-2.png'}]))
        assert html.count('<img') == 2

    def test_uri_is_escaped_not_interpolated_raw(self):
        """A data URI reaches the DOM as an attribute — a quote in it must not
        be able to break out and inject markup."""
        html = _render(_cmd_round(qrImages=[
            {'uri': 'data:image/png;base64,A"><script>x</script>',
             'format': 'png', 'filename': 'q.png'}]))
        assert '<script>' not in html
        assert '&quot;' in html or '&gt;' in html

    def test_failed_command_still_shows_its_qr(self):
        """Device-code flows often print the QR and then exit non-zero while
        waiting; the code must still be shown."""
        html = _render(_cmd_round(exitCode='1', qrImages=[
            {'uri': _URI, 'format': 'png', 'filename': 'qr.png'}]))
        assert 'ptool-qr-strip' in html
        assert 'ptool-cmd-err' in html
