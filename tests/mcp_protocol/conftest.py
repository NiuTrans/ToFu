"""Hermetic declaration authority for protocol-only tests.

Real configured-backend lifecycle/CAS coverage lives in
 tests/storage/test_mcp_declarations.py; no protocol test touches live storage.
"""
import copy
import threading

import pytest


@pytest.fixture(autouse=True)
def declaration_authority(monkeypatch):
    from lib.storage.mcp_declarations import MCPDeclarationStore

    class SettingsClient:
        def __init__(self):
            self.settings = {}
            self.lock = threading.Lock()

        def query(self, operation, payload):
            assert operation == 'conversation.get'
            key = (payload['user_id'], payload['conv_id'])
            with self.lock:
                return {'metadata': {'settings': copy.deepcopy(self.settings.get(key, {}))}}

        def command(self, operation, payload, command_id):
            assert operation == 'conversation.settings.update'
            key = (payload['user_id'], payload['conv_id'])
            with self.lock:
                if self.settings.get(key, {}) != payload['expected_settings']:
                    return {'applied': False, 'conflict': True}
                self.settings[key] = copy.deepcopy(payload['updates'])
                return {'applied': True}

    client = SettingsClient()
    monkeypatch.setattr(MCPDeclarationStore, '_storage', lambda self: client)
    return client
