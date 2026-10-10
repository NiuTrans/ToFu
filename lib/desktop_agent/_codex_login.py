"""Permission-gated, access-only Codex login snapshot for the paired server.

No caller-supplied paths, shell commands, logout, or secret-file writes.
Native Codex handles source discovery and renewal within the sharing grant.
The dispatch boundary requires the agent's explicit allow_codex_login grant.
"""
from lib.oauth.codex_local_login import read_local_login


def cmd_codex_login_read(params):
    if (not isinstance(params, dict) or set(params) - {'account_id'}
            or not isinstance(params.get('account_id', ''), str)
            or len(params.get('account_id', '')) > 256):
        return {'error': 'invalid_codex_login_request'}
    return read_local_login(expected_account_id=params.get('account_id', ''), refresh=True)
