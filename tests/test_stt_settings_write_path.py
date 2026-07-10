#!/usr/bin/env python3
"""The Speech-tab write shape → mic-appears, through the REAL slot build.

The Speech settings tab (static/js/settings/speech.js) writes a dedicated
``stt`` provider whose model carries an EXPLICIT per-(key,model)
``key_access[idx].capabilities`` override. This suite proves that shape
actually makes voice input available when built by the real dispatcher path
(``LLMDispatcher._build_slots_from_providers``) — NOT a stubbed
``_transcription_slots`` (which would hide the DEFAULT_SLOT_CONFIGS trap).

The trap (dispatcher.py): the final slot caps are
``set(cell_caps) if cell_caps is not None else set(alias_cfg.get('caps', model_caps))``
where ``alias_cfg = DEFAULT_SLOT_CONFIGS.get(model_id)``. So for a model that
IS in the reference table WITHOUT an audio cap (e.g. ``gpt-4o`` =
{text,vision,cheap}), a plain model-level ``capabilities: ['transcription']``
loses to the table — only the per-cell ``key_access`` override wins.

Biting neuter: the SAME provider built WITHOUT ``key_access`` (the pre-fix /
naive shape) using a trap model (``gpt-4o``) leaves
``transcription_available()`` False — proving the override is load-bearing.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_stt_settings_write_path.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]


# ── Mirror of settings/speech.js::_collectSttProvider write shape ──────────
# Kept faithful to the JS so this test guards the shape the tab actually ships.

_STT_META = {
    'openai': {'cap': 'transcription', 'needsKey': True,  'base': 'https://api.openai.com/v1',      'model': 'gpt-4o-transcribe'},
    'groq':   {'cap': 'transcription', 'needsKey': True,  'base': 'https://api.groq.com/openai/v1', 'model': 'whisper-large-v3-turbo'},
    'omni':   {'cap': 'audio_chat',    'needsKey': False, 'base': 'https://aigc.example/v1',        'model': 'gemini-3-flash-preview'},
    'custom': {'cap': 'transcription', 'needsKey': False, 'base': 'http://127.0.0.1:8000/v1',       'model': 'whisper-1'},
}


def _stt_provider(kind, *, key='sk-test', model=None, with_key_access=True):
    """Build the dedicated 'stt' provider exactly as the Speech tab does.

    Mirrors _collectSttProvider's FIXED contract: brand:'local' whenever the
    key is blank (so the dispatcher's no-keys skip doesn't drop the slot).
    """
    meta = _STT_META[kind]
    model = model or meta['model']
    api_keys = [key] if key else []
    n_keys = len(api_keys) or 1
    m = {
        'model_id': model,
        'aliases': [],
        'capabilities': [meta['cap']],
        'rpm': 60,
        'cost': 0.001,
    }
    if with_key_access:
        m['key_access'] = {str(i): {'capabilities': [meta['cap']]} for i in range(n_keys)}
    return [{
        'id': 'stt',
        'name': 'Speech',
        'base_url': meta['base'],
        'api_keys': api_keys,
        'enabled': True,
        # FIXED contract: keyless → brand:'local' so the slot is not skipped.
        'brand': 'local' if not api_keys else '',
        'models': [m],
    }]


def _dispatcher_with(providers):
    """Build a throwaway dispatcher's slots via the REAL slot-build path."""
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    d = LLMDispatcher()
    d.slots = []
    d._build_slots_from_providers(providers)
    d.initialize = lambda: None      # don't let discovery overwrite our slots
    return d


# ── The core loop: each card's write shape flips availability ──────────────

@pytest.mark.parametrize('kind,expected_mode', [
    ('openai', 'endpoint'),
    ('groq', 'endpoint'),
    ('omni', 'chat'),
    ('custom', 'endpoint'),
])
def test_tab_write_makes_transcription_available(kind, expected_mode, monkeypatch):
    import lib.transcription as tr
    d = _dispatcher_with(_stt_provider(kind))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)

    assert tr.transcription_available() is True, kind
    models = tr.list_transcription_models()
    assert models, kind
    assert models[0]['mode'] == expected_mode, (kind, models)


def test_custom_local_endpoint_without_key_still_available(monkeypatch):
    """A key-less local Custom endpoint (brand=local) still builds a slot."""
    import lib.transcription as tr
    d = _dispatcher_with(_stt_provider('custom', key=''))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    assert tr.transcription_available() is True


def test_omni_blank_key_gateway_auth_still_available(monkeypatch):
    """THE default-gateway path: Omni + blank key (reuse gateway auth) must
    flip transcription_available() True. The tab sets brand:'local' when the
    key is blank so `_build_slots_from_providers` does not skip the provider
    for having no API keys."""
    import lib.transcription as tr
    d = _dispatcher_with(_stt_provider('omni', key=''))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    assert tr.transcription_available() is True
    assert tr.list_transcription_models()[0]['mode'] == 'chat'


def test_neuter_omni_blank_key_without_local_brand_is_skipped(monkeypatch):
    """Neuter for the no-keys-skip bug: the SAME blank-key Omni provider but
    with brand:'' (the pre-fix shape) yields ZERO slots — the dispatcher skips
    a keyless non-local provider — so transcription stays UNAVAILABLE. Proves
    the brand:'local' assignment is load-bearing, not cosmetic."""
    import lib.transcription as tr
    prov = _stt_provider('omni', key='')
    prov[0]['brand'] = ''            # revert the fix
    d = _dispatcher_with(prov)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    assert tr.transcription_available() is False, \
        'keyless non-local provider is skipped by _build_slots_from_providers'


def test_endpoint_card_blank_key_never_configured_at_write(monkeypatch):
    """Contract at the WRITE path: a public cloud endpoint card (OpenAI/Groq)
    with a blank key is 'unconfigured' — the collect gate returns null so no
    dead slot is ever produced. We assert the intended shape: needsKey cards
    must carry a key, and if they don't the tab writes NOTHING. Here we verify
    the slot-build side: a keyed OpenAI provider is available (control), and a
    keyless one that somehow reached the builder is skipped (no silent 401)."""
    import lib.transcription as tr
    # Control: keyed OpenAI is available.
    d_ok = _dispatcher_with(_stt_provider('openai', key='sk-x'))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d_ok)
    assert tr.transcription_available() is True
    # The tab's gate returns null for a blank-key OpenAI card, so it never
    # reaches the builder. If it did (brand:'' keyless), it is skipped.
    prov = _stt_provider('openai', key='')
    prov[0]['brand'] = ''            # OpenAI is needsKey → tab wouldn't write local
    d_bad = _dispatcher_with(prov)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d_bad)
    assert tr.transcription_available() is False


def test_omni_card_uses_audio_chat_not_endpoint(monkeypatch):
    """The Omni card must tag audio_chat (inline chat), never 'transcription'
    (which would 404 on a gateway with no /audio/transcriptions)."""
    import lib.transcription as tr
    d = _dispatcher_with(_stt_provider('omni'))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d)
    slots = tr._transcription_slots()
    assert slots and 'audio_chat' in slots[0].capabilities
    assert 'transcription' not in slots[0].capabilities


# ── The biting neuter: drop key_access on a TRAP model → stays unavailable ──

def test_neuter_without_key_access_trap_model_stays_unavailable(monkeypatch):
    """DEFAULT_SLOT_CONFIGS trap: a model in the reference table WITHOUT an
    audio cap ('gpt-4o' = text/vision/cheap), given only a model-level
    capabilities list and NO key_access override, loses the cap at slot-build
    → transcription stays UNAVAILABLE. Proves the per-cell override is what
    makes the tab work."""
    from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS
    # Guard the premise: gpt-4o is a real trap entry lacking the audio cap.
    assert 'gpt-4o' in DEFAULT_SLOT_CONFIGS
    assert 'transcription' not in DEFAULT_SLOT_CONFIGS['gpt-4o']['caps']

    import lib.transcription as tr

    # NEUTER shape: no key_access, trap model → table caps win.
    d_bad = _dispatcher_with(
        _stt_provider('custom', model='gpt-4o', with_key_access=False))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d_bad)
    assert tr.transcription_available() is False, \
        'neuter should be unavailable — table caps overrode model caps'

    # FIXED shape (what the tab actually writes): key_access override wins.
    d_good = _dispatcher_with(
        _stt_provider('custom', model='gpt-4o', with_key_access=True))
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: d_good)
    assert tr.transcription_available() is True, \
        'key_access capability override must defeat DEFAULT_SLOT_CONFIGS'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
