"""Conversation settings contract: the server half.

contracts/conversation_settings_v1.json declares which settings keys cross the
browser boundary, who writes each one, and which projections carry it. These
checks prove that the resolver persists exactly the browser-written
vocabulary, that the catalog projection derives from the contract, that every
server-authored key still has its declared writer, and that private keys never
enter the public vocabulary. The browser half lives in
frontend/src/conversation/application/conversation-settings-contract.test.ts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from lib.conv_config import resolve_conv_settings
from lib.conversation_sync.settings_policy import (
    CATALOG_SETTING_KEYS,
    CONVERSATION_SETTINGS_CONTRACT,
    PRIVATE_CONVERSATION_SETTING_KEYS,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
KEYS = CONVERSATION_SETTINGS_CONTRACT['keys']
AUTHORS = set(CONVERSATION_SETTINGS_CONTRACT['authors'])
FIELDS = set(CONVERSATION_SETTINGS_CONTRACT['fields']) | {'author'}


def _declared(*authors: str) -> set[str]:
    return {key for key, spec in KEYS.items() if spec['author'] in authors}


def test_every_declaration_is_well_formed():
    for key, spec in KEYS.items():
        assert spec['author'] in AUTHORS, key
        assert set(spec) <= FIELDS, (key, sorted(set(spec) - FIELDS))
        if spec.get('catalog') or spec.get('cache') or spec.get('clearWhenAbsent'):
            assert spec.get('adopt') is True, (
                f'{key}: the browser must adopt every key it receives or caches')
        if spec['author'] == 'composer':
            assert spec.get('adopt') is True and spec.get('cache') is True, (
                f'{key}: composer state must round-trip and restore from cache')
        if spec['author'] in ('composer-derived', 'resolver-derived'):
            assert not any(spec.get(field) for field in ('adopt', 'catalog', 'cache')), key
        assert bool(spec.get('owners')) == (spec['author'] == 'server'), key
        for alias in spec.get('readAliases', ()):
            assert alias in KEYS and not KEYS[alias].get('adopt'), (key, alias)


def test_private_keys_never_enter_the_public_vocabulary():
    assert not set(KEYS) & PRIVATE_CONVERSATION_SETTING_KEYS


@pytest.mark.parametrize('key', sorted(_declared('server')))
def test_server_authored_keys_keep_their_declared_writers(key):
    for owner in KEYS[key]['owners']:
        source = (ROOT / owner).read_text(encoding='utf-8')
        assert f"'{key}'" in source or f'"{key}"' in source, (
            f'{owner} no longer writes {key}; change the contract and the '
            'browser adopter together')


def test_resolver_persists_exactly_the_browser_written_vocabulary():
    resolved = resolve_conv_settings({'chatMode': 'chat'}, {})

    assert set(resolved) == _declared('composer', 'composer-derived', 'resolver-derived')


def test_catalog_projection_is_derived_from_the_contract():
    from routes import conversations

    assert CATALOG_SETTING_KEYS == {key for key, spec in KEYS.items() if spec.get('catalog')}
    assert conversations.CATALOG_SETTING_KEYS is CATALOG_SETTING_KEYS
