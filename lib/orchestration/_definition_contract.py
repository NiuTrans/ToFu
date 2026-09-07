"""Canonical orchestration-definition identity and structural caps.

These constants are shared by builders, validators, wire schemas, inspection
and authoring clients. Keeping them independent of any implementation module
prevents a limit or schema identifier from drifting across those boundaries.
"""

from lib.orchestration.wire_formats import DEFINITION_FORMAT


SCHEMA_ID = DEFINITION_FORMAT


NODE_TYPE_ORDER = ('role', 'subflow', 'control')

MAX_NAME_LEN = 120
MAX_NODES = 200
