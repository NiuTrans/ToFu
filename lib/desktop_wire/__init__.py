"""Shared desktop bridge wire policy; stdlib-only, safe in frozen agents.

The schema owns version, admission limits and lifetime semantics. Server queue
and agent runtime consume it without importing one another's composition.
"""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def contract():
    return json.loads((Path(__file__).resolve().parents[2]
                       / 'contracts/desktop_bridge_v3.schema.json').read_text())


def protocol_version():
    return contract()['x-protocol-version']


def limit(name):
    return contract()['x-resource-limits'][name]
