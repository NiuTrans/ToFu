"""The row mirror must not maintain a duplicate of its composite PK index."""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_core_table_primary_key_covers_conversation_sequence():
    from lib.database._core_schema import CONVERSATION_MESSAGES

    assert [c.name for c in CONVERSATION_MESSAGES.primary_key.columns] == [
        'conv_id', 'seq']


@pytest.mark.parametrize('module_name', [
    'lib.database._schema_pg._chat',
    'lib.database._schema_sqlite._chat',
])
def test_schema_drops_legacy_duplicate_instead_of_recreating_it(module_name):
    module = __import__(module_name, fromlist=['_init_chat_schema'])
    src = inspect.getsource(module._init_chat_schema)
    assert 'DROP INDEX IF EXISTS idx_conv_msgs_conv' in src
    assert 'CREATE INDEX IF NOT EXISTS idx_conv_msgs_conv' not in src
