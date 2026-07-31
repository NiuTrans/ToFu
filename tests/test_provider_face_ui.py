#!/usr/bin/env python3
"""tests/test_provider_face_ui.py — the wire face must be VISIBLE and honest.

WHY THIS EXISTS
===============
``lib/llm_dispatch/provider_face.py`` has owned the account/face separation
since charter #23, and it works: measured on the live config, ``claude-opus-5``
resolves to ``https://api.openai.com/v1/anthropic`` while ``kimi-k3`` stays
on ``/v1/openai/native``. But the Settings UI rendered NONE of it, so the only
way to learn which wire a model used was to read ``server_config.json`` — the
question that opened this batch.

Three surfaces were added (model-card pill, per-model pin, provider faces{}
editor). This module pins the ONE property that makes them safe.

THE CENTRAL RULE
================
The family rule ("Claude belongs on the Anthropic wire") must exist EXACTLY
ONCE, in Python. The UI asks the backend
(``POST /api/v1/providers/resolve-faces`` → ``resolve_face``) and renders the
answer.

Why this matters more than it looks: a hand-written JS copy would not fail
loudly when it drifts. It would render a pill reading ``anthropic`` on a model
that actually dispatches over the OpenAI wire — which is precisely the silent
signature-dropping shape the resolver was built to prevent, now with a green
label on top of it. A wrong pill is worse than no pill. Charter #12 bans
hand-copied backend enums for the same reason.

So the guards below assert BOTH directions:
  * the endpoint delegates to the real resolver (not a re-derivation), and
  * the frontend contains NO independent claude→anthropic decision.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_provider_face_ui.py -v
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from tests._source_scan import strip_comments

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FACES_JS = os.path.join(_ROOT, 'static', 'js', 'settings', 'provider_faces.js')
_EDIT_JS = os.path.join(_ROOT, 'static', 'js', 'settings', 'model_edit.js')
_RENDER_JS = os.path.join(_ROOT, 'static', 'js', 'settings', 'provider_render.js')
_API_JS = os.path.join(_ROOT, 'static', 'js', 'api.js')
_ROUTES = os.path.join(_ROOT, 'routes', 'config.py')
_BUNDLER = os.path.join(_ROOT, 'lib', 'js_bundler.py')
_I18N = os.path.join(_ROOT, 'static', 'js', 'i18n.js')

GW = 'your-llm-gateway.example.com'
OPENAI_URL = 'https://api.openai.com/v1'
ANTHROPIC_URL = 'https://api.openai.com/v1/anthropic'

MERGED = {
    'id': 'default',
    'base_url': OPENAI_URL,
    'api_keys': ['k1'],
    'faces': {'anthropic': {'base_url': ANTHROPIC_URL, 'protocol': 'anthropic'}},
    'models': [
        {'model_id': 'kimi-k3'},
        {'model_id': 'claude-opus-5',
         'request_ids': ['yuju-claude-opus-5-evaDaily']},
        {'model_id': 'pinned-away', 'face': 'default'},
    ],
}

FACELESS = {
    'id': 'example-corp_old',
    'base_url': OPENAI_URL,
    'api_keys': ['k1'],
    'models': [{'model_id': 'claude-opus-5'}],
}


def _src(path, lang='js'):
    with open(path, encoding='utf-8') as f:
        return strip_comments(f.read(), lang=lang)


# ═══════════════════════════════════════════════════════════
#  1. The endpoint answers with the REAL resolver
# ═══════════════════════════════════════════════════════════

def _resolve_payload(provider):
    """POST to the REAL endpoint and return its JSON body.

    Deliberately NOT a re-implementation of the handler's loop. An earlier
    version of this helper rebuilt the response from ``resolve_face``
    directly, which made every assertion below blind to the handler itself:
    the skip-semantics added for the dispatcher's pre-resolve filters live
    in the HANDLER, so a local copy would have stayed green while the real
    endpoint said something else. Charter #24's rule (one implementation of
    the judgement) applies to test helpers too.
    """
    import asyncio

    async def _go():
        import server
        client = server.app.test_client()
        resp = await client.post('/api/v1/providers/resolve-faces',
                                 json={'provider': provider})
        assert resp.status_code == 200, resp.status_code
        return await resp.get_json()

    return asyncio.run(_go())


def test_endpoint_reports_the_anthropic_face_for_claude():
    """The pill's whole reason to exist: Claude must READ as anthropic."""
    payload = _resolve_payload(MERGED)
    by = {r['model_id']: r for r in payload['resolutions']}
    assert by['claude-opus-5']['protocol'] == 'anthropic'
    assert by['claude-opus-5']['base_url'] == ANTHROPIC_URL
    assert by['kimi-k3']['protocol'] in ('', 'openai')
    assert by['kimi-k3']['base_url'] == OPENAI_URL


def test_endpoint_flags_a_pin_as_forced():
    """A pin that overrode the family rule must be DISTINGUISHABLE, or the UI
    cannot tell 'the resolver chose this' from 'a human overrode it'."""
    by = {r['model_id']: r for r in _resolve_payload(MERGED)['resolutions']}
    assert by['pinned-away']['forced'] is True
    assert by['kimi-k3']['forced'] is False


def test_endpoint_surfaces_the_refusal_with_its_reason():
    """A refused model must carry a non-empty error — the chip's tooltip is
    the only place a user learns WHY the model vanished from the picker."""
    by = {r['model_id']: r for r in _resolve_payload(FACELESS)['resolutions']}
    rec = by['claude-opus-5']
    assert rec['ok'] is False
    assert 'anthropic' in rec['error'].lower()


# ═══════════════════════════════════════════════════════════
#  1b. ★ The endpoint must honour the dispatcher's PRE-RESOLVE filters
#
#  ``_build_slots_from_providers`` applies four semantic filters BEFORE it
#  ever calls resolve_face (measured on lib/llm_dispatch/dispatcher.py):
#
#      L334  provider.enabled is False        → whole card skipped
#      L399  effective_keys empty             → whole card skipped
#      L408  model_id empty                   → entry skipped
#      L414  model_entry.enabled is False     → entry skipped
#
#  A skipped entry never reaches the resolver, so it can never appear in
#  ``face_refusals``. The endpoint is the SECOND consumer of resolve_face
#  and must agree, or the UI paints an amber "not registered (missing wire
#  face)" banner on a model the user simply switched OFF — an alarm whose
#  stated cause is not the real one. Charter #26: a new consumer of a
#  filtered surface MUST inventory that filter.
#
#  Note on L399: the predicate is ``effective_keys``, NOT "has api_keys" —
#  a brand=='local' provider with no keys is given one blank-key slot and
#  DOES build. Keying this on api_keys alone would wrongly mark every
#  keyless local card as skipped.
# ═══════════════════════════════════════════════════════════

_CLAUDE_ON_FACELESS_GW = [{'model_id': 'claude-opus-5'}]


def test_disabled_model_is_not_reported_as_face_refused():
    """A model the USER turned off must not be blamed on a missing face."""
    prov = {'id': 'p', 'base_url': OPENAI_URL, 'api_keys': ['k'],
            'models': [{'model_id': 'claude-opus-5', 'enabled': False}]}
    rec = _resolve_payload(prov)['resolutions'][0]
    assert rec.get('skipped') == 'model_disabled', (
        'a disabled model must be reported as SKIPPED, not resolved — the '
        'dispatcher never resolves it (dispatcher.py L414)')
    assert rec['ok'] is not False or rec.get('skipped'), (
        'a disabled model must never surface as an unexplained refusal')


def test_disabled_provider_skips_every_model():
    """Whole-card off (dispatcher.py L334) — nothing on it is registered,
    so nothing on it can be 'refused for a missing face'."""
    prov = {'id': 'p', 'base_url': OPENAI_URL, 'enabled': False,
            'api_keys': ['k'], 'models': _CLAUDE_ON_FACELESS_GW}
    rec = _resolve_payload(prov)['resolutions'][0]
    assert rec.get('skipped') == 'provider_disabled'


def test_provider_without_usable_keys_skips_every_model():
    """dispatcher.py L399 — the owner's inventory missed this one; measured
    the same false-amber shape as the other two."""
    prov = {'id': 'p', 'base_url': OPENAI_URL, 'api_keys': [],
            'models': _CLAUDE_ON_FACELESS_GW}
    rec = _resolve_payload(prov)['resolutions'][0]
    assert rec.get('skipped') == 'no_keys'


def test_keyless_LOCAL_provider_is_NOT_skipped():
    """The complement, and the reason the predicate is `effective_keys`
    rather than `api_keys`: a self-hosted vLLM/SGLang card runs without
    auth and DOES build slots (dispatcher.py L401). Marking it skipped
    would blank the pills on every local deployment."""
    prov = {'id': 'p', 'base_url': 'http://10.0.0.5:8000/v1', 'brand': 'local',
            'api_keys': [], 'models': [{'model_id': 'qwen3-32b'}]}
    rec = _resolve_payload(prov)['resolutions'][0]
    assert not rec.get('skipped'), (
        'a keyless LOCAL provider builds slots — it must resolve normally')
    assert rec['ok'] is True


def test_a_genuinely_refused_model_is_still_reported():
    """NEUTER-complement: the skip logic must not swallow the REAL refusal
    it was built alongside. An ENABLED Claude entry on a faceless dual-face
    gateway must still come back ok=False with a reason."""
    rec = _resolve_payload(FACELESS)['resolutions'][0]
    assert not rec.get('skipped'), 'this entry is enabled — nothing to skip'
    assert rec['ok'] is False
    assert 'anthropic' in rec['error'].lower()


def test_face_list_is_backend_derived_and_default_first():
    """The pin dropdown's options come from here; 'default' must lead so the
    UI never has to hand-order (or hand-name) them."""
    payload = _resolve_payload(MERGED)
    assert payload['faces'][0] == 'default'
    assert 'anthropic' in payload['faces']


def test_route_is_registered_and_delegates_to_resolve_face():
    """The endpoint must CALL the shared resolver rather than re-deriving.

    Asserted on the parsed source (comments stripped, charter #24) so a
    future edit that inlines a local copy of the rule goes red here.
    """
    src = _src(_ROUTES, lang='python')
    assert "'/api/v1/providers/resolve-faces'" in src, 'route not registered'
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef)
               and n.name == 'resolve_provider_faces'), None)
    assert fn is not None, 'resolve_provider_faces handler missing'
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert 'resolve_face' in called, (
        'the endpoint must delegate to provider_face.resolve_face; a local '
        're-derivation would drift from what the dispatcher actually does')


# ═══════════════════════════════════════════════════════════
#  2. ★ The frontend must NOT re-implement the family rule
# ═══════════════════════════════════════════════════════════

def test_frontend_contains_no_second_family_rule():
    """THE guard this module exists for.

    A JS copy of "claude → anthropic" would not fail loudly when it drifts;
    it would mislabel a model that dispatches over the OpenAI wire. Scan the
    face-aware frontend files (comments stripped — a docstring explaining the
    rule must neither satisfy nor violate this) for any independent decision
    keying a claude-ish token against an anthropic-ish one.
    """
    offenders = []
    for path in (_FACES_JS, _EDIT_JS, _RENDER_JS):
        src = _src(path, lang='js')
        for i, line in enumerate(src.splitlines(), 1):
            low = line.lower()
            if 'claude' not in low:
                continue
            # A line naming claude is only acceptable if it does NOT also
            # decide a protocol/face from it.
            if re.search(r'anthropic|\.face\s*=|protocol\s*=', low):
                offenders.append('%s:%d: %s' % (os.path.basename(path), i,
                                                line.strip()[:110]))
    assert not offenders, (
        'the frontend appears to decide the wire face from the model name '
        'itself — that is a SECOND implementation of the family rule and it '
        'will drift from lib/llm_dispatch/provider_face.py, rendering a '
        '"anthropic" pill on a model that dispatches over the OpenAI wire:\n'
        + '\n'.join(offenders))


def test_chip_renders_only_from_a_landed_resolution():
    """A cache MISS must render NOTHING rather than a guess.

    An absent pill is honest ("not resolved yet"); a guessed one is a claim
    about routing the frontend is not entitled to make.
    """
    src = _src(_FACES_JS, lang='js')
    fn = src[src.index('function _faceChipHTML'):]
    fn = fn[:fn.index('\nfunction ')] if '\nfunction ' in fn else fn
    assert re.search(r'if\s*\(\s*!r\s*\)\s*return\s+[\'"]{2}', fn), (
        '_faceChipHTML must early-return an empty string when no resolution '
        'has landed for this model')


def test_ui_calls_the_endpoint_through_the_api_client():
    """Charter: every backend call goes through window.Api (api.js is the
    single seam). A raw fetch here would bypass auth/base-url handling."""
    api = _src(_API_JS, lang='js')
    assert 'resolveFaces' in api, 'Api.providers.resolveFaces not registered'
    assert '/api/v1/providers/resolve-faces' in api

    faces = _src(_FACES_JS, lang='js')
    assert 'Api.providers.resolveFaces' in faces
    assert 'fetch(' not in faces, (
        'provider_faces.js must not issue a raw fetch — route it through Api')


# ═══════════════════════════════════════════════════════════
#  3. The three surfaces are actually wired in
# ═══════════════════════════════════════════════════════════

def test_model_card_renders_the_face_chip():
    """Surface 1. Without this call the chip function is dead code."""
    src = _src(_RENDER_JS, lang='js')
    assert '_faceChipHTML(' in src, (
        'the model card never calls _faceChipHTML — the pill would never '
        'appear, which is the exact gap this batch set out to close')


def test_model_editor_exposes_the_pin_and_persists_it():
    """Surface 2: the pin must both RENDER and SAVE.

    A dropdown that renders but never writes `m.face` is the worst outcome:
    the user believes they pinned a face and routing ignores it.
    """
    src = _src(_EDIT_JS, lang='js')
    assert 'stg-edit-face' in src, 'no face pin control in the edit form'
    assert '_faceNamesFor(' in src, (
        'the pin options must come from the backend-derived face list, not '
        'a hand-written array')
    save = src[src.index('function _saveModelEdit'):]
    assert re.search(r'm\.face\s*=', save), 'the pin is never persisted'
    assert re.search(r'delete\s+m\.face', save), (
        "selecting 'automatic' must REMOVE the pin, not store an empty "
        'string — an empty face would be looked up as a face name')


def test_provider_card_exposes_the_faces_editor():
    """Surface 3. Before this, faces{} was writable only by template sync,
    so a self-built dual-face gateway needed a hand-edited JSON file."""
    render = _src(_RENDER_JS, lang='js')
    assert '_renderFacesSection(' in render, (
        'the provider card never renders the faces editor')
    faces = _src(_FACES_JS, lang='js')
    for fn in ('_renderFacesSection', '_addFace', '_deleteFace',
               '_collectFacesFromDom'):
        assert 'function %s' % fn in faces, 'missing %s' % fn


def test_faces_editor_refuses_to_redefine_the_default_face():
    """'default' IS the provider's own base_url/protocol. Accepting a row
    named 'default' would create a second, contradictory source for one
    face — the resolver would read faces['default'] while every other
    surface reads provider.base_url."""
    src = _src(_FACES_JS, lang='js')
    collect = src[src.index('function _collectFacesFromDom'):]
    collect = collect[:collect.index('\nfunction ')]
    assert re.search(r"n\s*===\s*'default'", collect), (
        '_collectFacesFromDom must skip a row named "default"')


def test_face_resolution_does_not_full_rerender():
    """The resolve round-trip is triggered BY an edit, so re-rendering the
    whole tab on its return would destroy the open form / half-typed row
    that caused it."""
    src = _src(_FACES_JS, lang='js')
    fn = src[src.index('async function _refreshFaceResolutions'):]
    fn = fn[:fn.index('\nfunction ')]
    assert '_renderProvidersTab()' not in fn, (
        '_refreshFaceResolutions must patch the chips in place '
        '(_repaintFaceChips), not re-render the whole providers tab')
    assert '_repaintFaceChips(' in fn


# ═══════════════════════════════════════════════════════════
#  4. Registration + i18n (a control nobody can see is dead)
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  3b. ★ The skip semantics must survive the FRONTEND leg
#
#  The Python guards above POST a full provider dict straight to the
#  endpoint. That is structurally BLIND to the payload-construction step:
#  _refreshFaceResolutions builds its own object, and if it omits the
#  fields the skip logic reads, the backend can never skip anything in
#  production while every backend test stays green. Measured: that is
#  exactly what happened — the first cut of this batch fixed the endpoint
#  and shipped a payload with no `enabled` on it at all.
#
#  So these drive the REAL shipped JS, not a re-description of it.
# ═══════════════════════════════════════════════════════════

def _node_eval(js_body, extra_files=()):
    """Run *js_body* after loading the real face JS into one shared scope.

    Mirrors how lib/js_bundler.py concatenates these files (all globals in
    one window scope), so the functions under test are the shipped ones.
    """
    parts = []
    for path in (_FACES_JS,) + tuple(extra_files):
        with open(path, encoding='utf-8') as f:
            parts.append(f.read())
    harness = '''
// ── minimal global surface the face JS touches ──
var window = globalThis;
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function t(k, v) { return k + (v ? JSON.stringify(v) : ''); }
function debugLog() {}
function Icon() { return ''; }
var document = { querySelector: function() { return null; },
                 querySelectorAll: function() { return []; } };
var _stgProviders = [];
var Api = { providers: { resolveFaces: async function(p) {
  globalThis.__sentPayload = p;
  return { ok: true, resolutions: [], faces: ['default'], dual_face_host: false };
} } };
''' + '\n'.join(parts) + '\n' + js_body
    proc = subprocess.run(['node', '--input-type=module', '-e', harness],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        'node harness failed:\nSTDOUT:%s\nSTDERR:%s' % (proc.stdout, proc.stderr))
    return proc.stdout


def test_payload_carries_the_fields_the_skip_logic_reads():
    """★ THE cross-leg guard.

    The endpoint decides `skipped` from provider.enabled, the model's
    `enabled`, and whether the card has usable keys. If the frontend does
    not SEND those, the backend's skip branch is unreachable in production
    and every user-visible symptom of this epic survives — with a fully
    green backend suite.

    Note the credential discipline: the endpoint promises it never receives
    keys, so the payload carries a COUNT (plus brand, for the local
    blank-key rule), never the key strings themselves.
    """
    out = _node_eval('''
_stgProviders = [{
  id: 'p', base_url: 'https://api.openai.com/v1',
  enabled: false, brand: 'cloud', api_keys: ['sk-SECRET-1', 'sk-SECRET-2'],
  faces: {}, models: [{ model_id: 'claude-opus-5', enabled: false }],
}];
await _refreshFaceResolutions(0);
const p = globalThis.__sentPayload;
console.log(JSON.stringify({
  providerEnabled: p.enabled,
  modelEnabled: p.models[0].enabled,
  keyCount: p.api_key_count,
  brand: p.brand,
  leaked: JSON.stringify(p).includes('SECRET'),
}));
''')
    got = json.loads(out.strip().splitlines()[-1])
    assert got['providerEnabled'] is False, (
        "payload omits provider.enabled — the endpoint's provider_disabled "
        'branch is unreachable from the real UI')
    assert got['modelEnabled'] is False, (
        "payload omits the model's enabled flag — the model_disabled branch "
        'is unreachable from the real UI')
    assert got['keyCount'] == 2, (
        'payload must carry how many keys the card has (the no_keys branch '
        'mirrors dispatcher.py L399)')
    assert got['brand'] == 'cloud', (
        'brand is required: a keyless brand==local card still builds slots, '
        'so no_keys must not fire for it')
    assert got['leaked'] is False, (
        'API keys must NEVER travel to this endpoint — send a count')


def test_chip_is_blank_for_a_skipped_model():
    """A skipped entry builds no slot, so it has no wire face to report.

    Rendering the resolver's placeholder verdict would put a routing label
    on a model that is not routed at all — the same class of false claim as
    the amber refusal this epic removes, just quieter.
    """
    out = _node_eval('''
_stgProviders = [{ id: 'p', faces: { anthropic: {} }, models: [] }];
_stgFaceResolutions[0] = { byModel: {
  off:  { model_id: 'off',  skipped: 'model_disabled', ok: true,
          face: 'default', protocol: '', base_url: '', forced: false, error: '' },
  gone: { model_id: 'gone', skipped: 'provider_disabled', ok: false,
          face: 'default', protocol: '', base_url: '', forced: false,
          error: 'should not be shown' },
  live: { model_id: 'live', ok: true, face: 'anthropic',
          protocol: 'anthropic', base_url: 'https://x/v1/anthropic',
          forced: false, error: '' },
}, faces: ['default', 'anthropic'], dualFaceHost: true };
console.log(JSON.stringify({
  off:  _faceChipHTML(0, { model_id: 'off' }),
  gone: _faceChipHTML(0, { model_id: 'gone' }),
  live: _faceChipHTML(0, { model_id: 'live' }),
}));
''')
    got = json.loads(out.strip().splitlines()[-1])
    assert got['off'] == '', (
        'a disabled model must render NO face chip — it builds no slot')
    assert got['gone'] == '', (
        'a model on a disabled card must render no chip, and above all no '
        'refused chip: the amber alarm would name a cause that is not real')
    assert 'refused' not in got['gone']
    assert 'anthropic' in got['live'], (
        'NEUTER-complement: a genuinely resolved model must still get its '
        'pill — the skip branch must not blank everything')


def test_provider_faces_js_is_bundled():
    """An unbundled file is invisible to users no matter how correct it is."""
    src = _src(_BUNDLER, lang='python')
    assert "'settings/provider_faces.js'" in src, (
        'provider_faces.js is not in _BUNDLE_FILES — none of the three '
        'surfaces would load')


def test_every_new_i18n_key_has_both_languages():
    """A missing key renders as its literal dotted name (measured before:
    a literal `project.qrScan` shipped to users)."""
    with open(_I18N, encoding='utf-8') as f:
        i18n = f.read()
    keys = [
        'settings.faceChipRefused', 'settings.faceChipTitle',
        'settings.faceChipPinnedTag', 'settings.faceChipPinnedTitle',
        'settings.meFace', 'settings.meFaceHint', 'settings.meFaceAuto',
        'settings.meFacePinWarn', 'settings.wireFaces', 'settings.wireFacesHint',
        'settings.addFace', 'settings.addFaceTitle', 'settings.noFaces',
        'settings.faceNamePlaceholder', 'settings.deleteFaceTitle',
        'settings.faceDeleteConfirm',
    ]
    missing = []
    for k in keys:
        # Match to END OF LINE, not to the first '}': these values contain
        # placeholders like {face} / {url}, and a lazy [^}]* body stops
        # INSIDE the placeholder — reporting a complete entry as missing its
        # en half. (Measured: faceChipTitle + faceDeleteConfirm both have
        # zh AND en, but the naive regex captured only "zh: '协议面：{face".)
        m = re.search(r"^\s*'%s':\s*\{.*$" % re.escape(k), i18n, re.M)
        if not m:
            missing.append('%s (absent)' % k)
            continue
        body = m.group(0)
        if 'zh:' not in body or 'en:' not in body:
            missing.append('%s (missing zh or en)' % k)
    assert not missing, 'i18n keys incomplete: %s' % missing


def test_referenced_i18n_keys_all_exist():
    """Complement of the above: every settings.* key the new UI ASKS for must
    be defined. Catches a typo'd key, which renders as raw dotted text."""
    with open(_I18N, encoding='utf-8') as f:
        i18n = f.read()
    src = _src(_FACES_JS, lang='js')
    used = set(re.findall(r"t\(\s*'(settings\.[A-Za-z0-9_]+)'", src))
    assert used, 'no i18n keys found in provider_faces.js — scan is vacuous'
    missing = [k for k in sorted(used)
               if ("'%s':" % k) not in i18n]
    assert not missing, 'provider_faces.js uses undefined i18n keys: %s' % missing


def test_chip_and_warning_styles_exist():
    """A chip with no CSS class definition renders as unstyled text."""
    with open(os.path.join(_ROOT, 'static', 'styles.css'), encoding='utf-8') as f:
        css = f.read()
    for sel in ('.stg-face-chip', '.stg-face-chip.anthropic',
                '.stg-face-chip.refused', '.stg-face-warn', '.stg-face-row'):
        assert sel in css, 'missing CSS for %s' % sel


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
