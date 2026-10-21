"""Model-only native artifact pages: declared omission, never domain filtering.

Canonical/decoded results remain exact. Unknown fields, all source identities,
coverage/scope, error boundaries, media descriptors and next-page cursors survive.
Known rows inherit a reference only when it exactly equals their page reference.
The signed replay cursor stays in canonical evidence; clipped or unknown model
views retain it inline. Page pagination is independent of model-view clipping.
Read bodies may use the shared exact text framing when it reduces encoded size.
"""
from __future__ import annotations
from lib.tools.result_projection_json import (
    JSONNumber, dump_projection_json, load_projection_json,
)
from lib.tools.result_surfaces_generated import CONTRACT
from lib.tools.result_projection_text import compact_text_field

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
        provenance = value.get('provenance', [])
        return (type(value.get('content')) is str and _integer(value.get('offset'))
                and isinstance(provenance, list)
                and all(isinstance(row, dict) and type(row.get('artifactRef')) is str
                        for row in provenance))
    return (type(value.get('query')) is str and isinstance(value.get('items'), list)
            and all(isinstance(match, dict) and _integer(match.get('offset'))
                    and type(match.get('text')) is str
                    and type(match.get('artifactRef')) is str for match in value['items']))


def _compact_matches(matches):
    """Group consecutive equal quotations without merging source identities.

    Only the declared native fields participate. An unknown field, including
    one using the grouped-offset name, keeps its original per-hit shape.
    Offsets retain order, multiplicity and their exact numeric spelling.
    """
    policy = CONTRACT['artifactProjectionMatchGroups']
    offsets_field = policy['offsetsField']
    allowed = {'offset', *policy['sharedFields']}
    result = []
    previous = None
    for match in matches:
        path = match.get('path', [])
        known = (set(match) <= allowed and isinstance(path, list)
                 and all(type(part) is str for part in path))
        shared = {key: item for key, item in match.items() if key != 'offset'} if known else None
        if shared is not None and shared == previous:
            last = result[-1]
            if offsets_field not in last:
                last = {**shared, offsets_field: [last['offset']]}
                result[-1] = last
            last[offsets_field].append(match['offset'])
        else:
            result.append(match)
        previous = shared
    return result


def _compact_references(name, key, rows, parent_ref):
    """Omit only an equality-proven page reference from declared native rows."""
    policy = CONTRACT['artifactProjectionInheritedReferences']
    allowed = policy['containers'].get(name, {}).get(key)
    if not allowed or not isinstance(rows, list) or not parent_ref:
        return rows
    allowed = set(allowed)
    field = policy['field']
    result = []
    for row in rows:
        known = (isinstance(row, dict) and row.get(field) == parent_ref
                 and set(row) <= allowed)
        if known and key == 'provenance':
            known = (isinstance(row.get('path'), list)
                     and isinstance(row.get('segment'), list)
                     and all(_integer(part) for part in row['segment'])
                     and _integer(row.get('start')) and _integer(row.get('end')))
        elif known:
            path = row.get('path', [])
            known = (type(row.get('text')) is str and isinstance(path, list)
                     and all(type(part) is str for part in path)
                     and ((_integer(row.get('offset')) and 'offsets' not in row)
                          or ('offset' not in row and isinstance(row.get('offsets'), list)
                              and bool(row['offsets'])
                              and all(_integer(offset) for offset in row['offsets']))))
        result.append({key: value for key, value in row.items() if key != field}
                      if known else row)
    return result


def _compact_page(name, value):
    defaults = CONTRACT['artifactProjectionDefaults']
    replay = CONTRACT['artifactProjectionReplayCursor']
    clipped = value.get(replay['viewTruncatedField'], False)
    complete_view = type(clipped) is bool and not clipped
    partial = value.get('truncated') is True or value.get('outputTruncated') is True
    result = {}
    for key, item in value.items():
        if (key == replay['field'] and complete_view and type(item) is str
                and item.startswith(tuple(replay['formatPrefixes']))):
            # This cursor replays an already-delivered page; nextCursor alone
            # advances pagination. Retention captures the full source before
            # projection, including this cursor for later compaction recovery.
            continue
        if key in defaults and type(item) is type(defaults[key]) and item == defaults[key]:
            # Complete-view omission never conflates source coverage with page
            # truncation. Preserve all explicit gaps/continuations on partials.
            if not partial:
                continue
        projected = (_compact_matches(item)
                     if name == 'search_tool_artifact' and key == 'items' else item)
        result[key] = _compact_references(name, key, projected, value['artifactRef'])
    return result


def _smaller_text(candidate, original):
    return candidate if len(candidate.encode('utf-8')) < len(original.encode('utf-8')) else original


def _read_page_text(value, original=None):
    if value.get('status') != 'ok':
        return original if original is not None else dump_projection_json(value)
    policy = CONTRACT['artifactProjectionReadText']
    return compact_text_field(value, policy['bodyField'], original=original)


def _render(name, value, *, batch=False):
    original = dump_projection_json(value)
    if name != 'read_tool_artifact':
        return original
    if not batch:
        return _read_page_text(value, original)
    lines = [dump_projection_json({key: item for key, item in value.items() if key != 'items'})]
    for index, page in enumerate(value['items']):
        lines.extend((f'\n## items[{index}]', _read_page_text(page)))
    return _smaller_text('\n'.join(lines), original)


def project_artifact_result(tool_name: str, text: str) -> str | None:
    """Recognize a native page/indexed batch; retain unsupported/error-only text."""
    if tool_name not in _NAMES or not isinstance(text, str):
        return None
    try:
        value = load_projection_json(text)
        if _page(tool_name, value):
            return _render(tool_name, _compact_page(tool_name, value))
        if (not isinstance(value, dict) or value.get('status') not in {'ok', 'partial_failure'}
                or 'contractVersion' in value or not isinstance(value.get('items'), list)):
            return None
        pages = value['items']
        if not pages or not all(isinstance(page, dict) and _integer(page.get('index'))
                                and (_page(tool_name, page) or _error(page)) for page in pages):
            return None
        # Each page proves inheritance against its own reference. Another
        # retained child always keeps its explicit identity; no recursive walk.
        projected = {**value, 'items': [_compact_page(tool_name, page) if _page(tool_name, page) else page
                                      for page in pages]}
        return _render(tool_name, projected, batch=True)
    except (ValueError, TypeError, RecursionError, OverflowError):
        return None
