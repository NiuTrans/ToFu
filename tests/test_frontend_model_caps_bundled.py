"""Guard the model-capability owner in the retained Vite runtime."""

from __future__ import annotations

import pytest

from tests._runtime_sections import runtime_section, runtime_section_names

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_MODEL_CAPS_SIGNATURES = (
    'runtimeScope.getChatExcludedCaps',
    'runtimeScope.CHAT_EXCLUDED_CAPS_FALLBACK',
)


def test_bundle_contains_model_caps_signature_globals():
    source = runtime_section('core/model_caps.js')
    for signature in _MODEL_CAPS_SIGNATURES:
        assert signature in source


def test_bundle_manifest_lists_model_caps():
    names = runtime_section_names()
    assert names.count('core/model_caps.js') == 1
    assert names.index('core/model_caps.js') < names.index('main/main_toolbar_ui.js')


def test_neuter_removing_model_caps_from_manifest_breaks_presence():
    neutered = [name for name in runtime_section_names()
                if name != 'core/model_caps.js']
    combined = '\n'.join(runtime_section(name) for name in neutered)
    for signature in _MODEL_CAPS_SIGNATURES:
        assert signature not in combined
