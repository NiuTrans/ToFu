"""Model-only native artifact pages: declared defaults, never domain filtering.

Canonical/decoded results remain exact. Unknown fields, all source references,
coverage/scope, error boundaries, media descriptors and signed cursors survive.
Only known complete-view defaults can be omitted. Partial pages keep even the
zero resume point so clipping never strands historical evidence.
"""
from __future__ import annotations
from lib.tools.result_projection_json import (
    JSONNumber, dump_projection_json, load_projection_json,
)
from lib.tools.result_surfaces_generated import CONTRACT

_NAMES = {'read_tool_artifact', 'search_tool_artifact'}
_COVERAGES = {'complete', 'incomplete', 'unavailable'}


def _integer(value):
    # These native schemas require nonnegative integer offsets/indexes. Do not
    # normalize exotic numeric syntax or round fields belonging to applications.
    return isinstance(value, JSONNumber) and str(value).isdigit()


def _error(value):
    return (isinstance(value, dict) and value.get('status') == 'error'
            and isinstance(value.get('error'), dict)
            and type(value['error'].get('code')) is str)


def _page(name, value):
    if (not isinstance(value, dict) or value.get('status') != 'ok'
            or type(value.get('artifactRef')) is not str
            or type(value.get('coverage')) is not str
            or value['coverage'] not in _COVERAGES
            or type(value.get('truncated')) is not bool
            or (value.get('nextCursor') is not None and type(value.get('nextCursor')) is not str)):
        return False
    if name == 'read_tool_artifact':
        return type(value.get('content')) is str and _integer(value.get('offset'))
    return (type(value.get('query')) is str and isinstance(value.get('items'), list)
            and all(isinstance(match, dict) and _integer(match.get('offset'))
                    and type(match.get('text')) is str
                    and type(match.get('artifactRef')) is str for match in value['items']))


def _compact_page(value):
    defaults = CONTRACT['artifactProjectionDefaults']
    partial = value.get('truncated') is True or value.get('outputTruncated') is True
    result = {}
    for key, item in value.items():
        if key in defaults and type(item) is type(defaults[key]) and item == defaults[key]:
            # Complete-view omission never conflates source coverage with page
            # truncation. Preserve all explicit gaps/continuations on partials.
            if not partial:
                continue
        result[key] = item
    return result


def project_artifact_result(tool_name: str, text: str) -> str | None:
    """Recognize a native page/indexed batch; retain unsupported/error-only text."""
    if tool_name not in _NAMES or not isinstance(text, str):
        return None
    try:
        value = load_projection_json(text)
        if _page(tool_name, value):
            return dump_projection_json(_compact_page(value))
        if (not isinstance(value, dict) or value.get('status') not in {'ok', 'partial_failure'}
                or 'contractVersion' in value or not isinstance(value.get('items'), list)):
            return None
        pages = value['items']
        if not pages or not all(isinstance(page, dict) and _integer(page.get('index'))
                                and (_page(tool_name, page) or _error(page)) for page in pages):
            return None
        # No recursive filtering or ref hoisting: a match can identify another
        # retained child, even when its parent and adjacent request share a ref.
        projected = {**value, 'items': [_compact_page(page) if _page(tool_name, page) else page
                                      for page in pages]}
        return dump_projection_json(projected)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
