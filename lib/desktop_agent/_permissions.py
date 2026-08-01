"""
Desktop Agent — permission policy (pure, no I/O).

Computer-control is powerful: with everything on, the local bridge can run
arbitrary shell commands, move the mouse and delete files on the user's own
machine with no per-action confirmation. The safety posture is therefore
**deny by default** — enabling computer control grants only READ-ONLY tools
(list/read files, screenshot-less system overview); the write / exec / GUI
tiers must each be turned on EXPLICITLY (via the tray toggles or the CLI
--allow-* flags).

This module is pure data + a builder so the policy can be unit-tested without
starting the agent:

  * ``SAFE_DEFAULT`` — the deny-all baseline (read-only).
  * ``build_permissions(...)`` — normalise CLI/tray inputs into the dict shape
    ``dispatch_command`` consumes, with ``allow_all`` as the master override.
  * ``PERMISSION_KEYS`` — the canonical tier names.
"""

PERMISSION_KEYS = ('allow_write', 'allow_exec', 'allow_gui', 'allow_egress')

# Deny-by-default: enabling the agent alone grants ONLY read-only tools.
SAFE_DEFAULT = {
    'allow_write': False,
    'allow_exec': False,
    'allow_gui': False,
    'allow_egress': False,
}


def build_permissions(allow_write=False, allow_exec=False,
                      allow_gui=False, allow_egress=False, allow_all=False) -> dict:
    """Return a normalised permissions dict.

    ``allow_all`` is a master override that turns on every tier. Each
    individual flag is coerced to a plain bool so a truthy CLI/UI value (e.g.
    argparse's store_true, or a tk BooleanVar) always yields ``True``/``False``.
    """
    master = bool(allow_all)
    return {
        'allow_write': master or bool(allow_write),
        'allow_exec': master or bool(allow_exec),
        'allow_gui': master or bool(allow_gui),
        'allow_egress': master or bool(allow_egress),
    }


def safe_default() -> dict:
    """Return a fresh copy of the deny-all baseline (safe to mutate)."""
    return dict(SAFE_DEFAULT)
