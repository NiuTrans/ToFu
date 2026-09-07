"""lib/oauth/pkce.py — PKCE (Proof Key for Code Exchange) helpers.

Implements RFC 7636 for OAuth 2.0 PKCE flow.
"""

import base64
import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['generate_pkce_codes']


def generate_pkce_codes() -> dict:
    """Generate a PKCE code verifier and challenge pair (RFC 7636).

    Returns:
        dict with 'code_verifier' and 'code_challenge' keys.
    """
    # Generate 96 bytes of random data for the verifier (same as CLIProxyAPI)
    verifier_bytes = os.urandom(96)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b'=').decode('ascii')

    # SHA256 hash of verifier → base64url-encoded challenge
    digest = hashlib.sha256(code_verifier.encode('ascii')).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode('ascii')

    logger.debug('[PKCE] Generated codes (verifier_len=%d, challenge_len=%d)',
                 len(code_verifier), len(code_challenge))
    return {
        'code_verifier': code_verifier,
        'code_challenge': code_challenge,
    }
