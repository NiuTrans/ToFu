"""Canonical source bindings; opaque logical document IDs are allocated by storage.

The key locates an existing binding without filesystem access. Display labels,
edited-file paths and compiler targets do not define document identity.
"""
from __future__ import annotations

import hashlib
import json

from .contract import validate


def source_binding(source: dict) -> dict:
    validate(source, 'PaperStoredSourceV1')
    names = {'local': ('source', 'project_root', 'main_file'),
             'overleaf': ('source', 'mcp_server', 'project_id', 'main_file'),
             'research': ('source', 'paper_hash', 'lang')}[source['source']]
    return {name: source[name] for name in names}


def source_binding_key(source: dict) -> str:
    return hashlib.sha256(json.dumps(source_binding(source), sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()
