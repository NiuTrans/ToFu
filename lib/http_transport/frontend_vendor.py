"""Read-only verification of the generated standalone browser dependency graph.

Entry: vendor_asset_mismatches(root), used by project diagnostics. The package
lock owns versions; generated_frontend_vendor owns the expected file digests.
No npm installation, application store or network is needed at runtime.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from lib.http_transport.generated_frontend_vendor import KATEX_STATIC_SHA256


def vendor_asset_mismatches(root: Path) -> list[str]:
    """Report every missing, unreadable or altered declared asset."""
    mismatches = []
    for relative, expected in KATEX_STATIC_SHA256.items():
        try:
            actual = sha256((root / relative).read_bytes()).hexdigest()
        except OSError as error:
            mismatches.append(f'{relative}: {error.strerror or type(error).__name__}')
        else:
            if actual != expected:
                mismatches.append(f'{relative}: differs from locked dependency bytes')
    return mismatches
