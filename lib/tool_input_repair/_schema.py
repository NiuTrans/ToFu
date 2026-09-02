"""Tool-schema index and type introspection helpers for tool-arg repair.

Walks declared schema-owner modules once to build ``{tool_name: parameters}``
and exposes the small pure accessors the repair passes anchor on
(``_expected_types``, ``_required_keys``, ``_array_item_schema``). The
``RepairLog`` public type alias also lives here so every submodule can import
it without a cycle.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ── Public type alias ──
RepairLog = list[tuple[str, str]]  # [(json_path, pattern_name), ...]


def _schema_owner_modules():
    """Return the explicit built-in modules that own wire tool schemas."""
    import lib.tools.browser as browser
    import lib.tools.code_exec as code_exec
    import lib.tools.conversation as conversation
    import lib.tools.human_guidance as human_guidance
    import lib.tools.image_edit as image_edit
    import lib.tools.image_gen as image_gen
    import lib.tools.motion_video as motion_video
    import lib.tools.project as project
    import lib.tools.search as search
    import lib.tools.tool_result_artifacts as tool_result_artifacts

    return (
        browser,
        code_exec,
        conversation,
        human_guidance,
        image_edit,
        image_gen,
        motion_video,
        project,
        search,
        tool_result_artifacts,
    )


def _build_schema_index() -> dict[str, dict[str, Any]]:
    """Walk declared schema owners and return the repair schema index.

    The map is built once at import time. New tools added later won't be
    seen until the process restarts; that matches how every other tool
    registry in this codebase works (PROJECT_TOOL_NAMES, etc.).

    Returns an empty dict on any failure — repair becomes a no-op rather
    than blocking startup.
    """
    index: dict[str, dict[str, Any]] = {}
    try:
        candidates: list[Any] = []
        for owner_module in _schema_owner_modules():
            for attr in dir(owner_module):
                obj = getattr(owner_module, attr, None)
                if isinstance(obj, list):
                    candidates.extend(obj)
                    continue
                if isinstance(obj, dict) and obj.get('type') == 'function':
                    candidates.append(obj)
                    continue
                if not (callable(obj) and attr.startswith('build_')):
                    continue
                # Zero-arg schema BUILDERS (e.g. build_search_tool): when a
                # static module-level schema dict becomes runtime-built, the
                # attr walk above goes blind and the repair for that tool
                # silently dies (the bare-string ``queries`` coercion was lost
                # exactly this way when SEARCH_TOOL_MULTI became a builder).
                # Guarded: a builder that raises (or needs args) is skipped,
                # never breaks the walk.
                try:
                    built = obj()
                except Exception as e:
                    logger.debug(
                        '[ToolRepair] builder %s.%s() skipped: %s',
                        owner_module.__name__, attr, e,
                    )
                    continue
                if isinstance(built, list):
                    candidates.extend(built)
                elif isinstance(built, dict) and built.get('type') == 'function':
                    candidates.append(built)
        for entry in candidates:
            if not isinstance(entry, dict):
                continue
            fn = entry.get('function') or {}
            name = fn.get('name')
            params = fn.get('parameters')
            if name and isinstance(params, dict):
                index[name] = params
    except Exception as e:
        logger.warning('[ToolRepair] Schema index build failed — repair disabled: %s', e)
    logger.info('[ToolRepair] Indexed %d tool schemas', len(index))
    return index


_SCHEMA_INDEX: dict[str, dict[str, Any]] | None = None


def _schemas() -> dict[str, dict[str, Any]]:
    global _SCHEMA_INDEX
    if _SCHEMA_INDEX is None:
        _SCHEMA_INDEX = _build_schema_index()
    return _SCHEMA_INDEX


def _expected_types(tool_name: str) -> dict[str, str]:
    """Return ``{property_name: json_schema_type}`` for one tool.

    Only top-level properties are returned — nested-object repair is
    intentionally out of scope (the value is mostly in the four
    deterministic top-level patterns; deep walks risk over-repair of
    legitimate user data like the ``content`` of a ``write_file`` call).
    """
    params = _schemas().get(tool_name)
    if not params:
        return {}
    props = params.get('properties') or {}
    out: dict[str, str] = {}
    for key, spec in props.items():
        if not isinstance(spec, dict):
            continue
        t = spec.get('type')
        if isinstance(t, str):
            out[key] = t
    return out


def _required_keys(tool_name: str) -> set[str]:
    params = _schemas().get(tool_name) or {}
    req = params.get('required') or []
    return set(req) if isinstance(req, list) else set()


def _array_item_schema(tool_name: str, key: str) -> tuple[dict[str, str], set[str]] | None:
    """Return ``({item_key: json_type}, required_item_keys)`` for an
    array-typed parameter whose items are objects with a declared schema, or
    ``None`` when the parameter isn't an array-of-objects we can anchor on.
    """
    params = _schemas().get(tool_name) or {}
    spec = (params.get('properties') or {}).get(key)
    if not isinstance(spec, dict) or spec.get('type') != 'array':
        return None
    items = spec.get('items')
    if not isinstance(items, dict):
        return None
    props = items.get('properties')
    if not isinstance(props, dict) or not props:
        return None
    types = {k: v.get('type') for k, v in props.items()
             if isinstance(v, dict) and isinstance(v.get('type'), str)}
    if not types:
        return None
    req = items.get('required') or []
    return types, (set(req) if isinstance(req, list) else set())
