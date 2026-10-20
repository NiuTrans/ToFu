"""routes/api_v1/project.py — Project Co-Pilot REST surface.

Routes (legacy snake_case path → new hyphen-case path):
  POST   /api/v1/project/set                — set primary project root
  PUT    /api/v1/project/paths              — atomically set primary + extras
  GET    /api/v1/project/status             — current project state
  DELETE /api/v1/project                    — clear active project
  POST   /api/v1/project/browse             — list a directory
  POST   /api/v1/project/mkdir              — create a new folder
  POST   /api/v1/project/rmdir              — delete a folder (→ trash bin)
  GET    /api/v1/project/recent             — list recent project paths
  POST   /api/v1/project/recent             — save a recent path
  DELETE /api/v1/project/recent             — clear recent list
  POST   /api/v1/project/recent/relink      — re-key a renamed/moved project path
  POST   /api/v1/project/git-root-hint      — nearest enclosing .git root for a path
  POST   /api/v1/project/write-approval     — resolve a pending write
  POST   /api/v1/project/undo               — per-round / per-conv undo
  POST   /api/v1/project/undo-all           — undo all pending mods in one project
  GET    /api/v1/project/gitignore/suggestions — pending suggestions
  POST   /api/v1/project/gitignore/accept   — append dirs to .gitignore
  POST   /api/v1/project/gitignore/dismiss  — drop suggestions
  POST   /api/v1/project/rescan             — refresh file index
  POST   /api/v1/project/redo               — re-apply previously-undone round
  POST   /api/v1/project/write              — direct file write (Apply Code)
  POST   /api/v1/project/upload            — save a dropped file into a folder (binary-safe)
  Signal-driven Project Brain routes live in routes/api_v1/project_brain.py.

All require ``@require_auth``. Authorized operations have no endpoint/IP
request-frequency quota; domain services own execution and recovery.

TODO(enterprise, I7): project selection, board, and charter state are
instance-global behind bare ``@require_auth`` — any authenticated user can
retarget the shared project root. Key all state by ``auth_ctx.user_id``.
docs/ENTERPRISE_EVOLUTION.md
"""

from __future__ import annotations

import os
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)

from quart import Blueprint, request

from lib.quart_sync import request_files, request_form

from lib.api_response import (
    api_bad_request,
    api_error,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.log import get_logger
from lib.openapi import api_meta
from lib.project_contracts.browse import browse_request_schema, normalize_browse_request
from lib.project_recent_contract import (
    RECENT_PROJECT_PATH_MAX_CHARS,
    RECENT_PROJECT_TOUCH_BATCH_LIMIT,
)
from lib.human_gate_contract import MAX_HUMAN_GATE_REQUEST_ID_LENGTH
from lib.request_parser import optional_bool, optional_str, parse_body

from .auth import request_user_id as _request_user_id, require_auth

logger = get_logger(__name__)

api_v1_project_bp = Blueprint("api_v1_project", __name__)


def _active_project_path(explicit: str = "") -> str:
    """Resolve the project path: explicit body field overrides server state."""
    if explicit:
        return explicit
    from lib.project_mod.config import _state

    return _state.get("path", "") or ""


def _project_set_recent_paths(data: dict, paths: list) -> list[str]:
    """Validate and canonicalize the optional same-request recent intent."""
    if "recentPaths" not in data:
        return []
    candidates = data.get("recentPaths")
    if (
        not isinstance(candidates, list)
        or len(candidates) > RECENT_PROJECT_TOUCH_BATCH_LIMIT
    ):
        raise ValueError(
            f"recentPaths must contain at most "
            f"{RECENT_PROJECT_TOUCH_BATCH_LIMIT} paths"
        )
    canonical: list[str] = []
    for candidate in candidates:
        if (
            not isinstance(candidate, str)
            or not candidate
            or len(candidate) > RECENT_PROJECT_PATH_MAX_CHARS
            or candidate not in paths
        ):
            raise ValueError("recentPaths must be a bounded subset of paths")
        path = os.path.abspath(os.path.expanduser(candidate))
        if path not in canonical:
            canonical.append(path)
    return canonical


def _decoded_path_arg(name: str = "path") -> str:
    """Read a project path query arg, defensively undoing proxy double-encoding.

    Thin wrapper over the shared ``lib.request_parser.decode_proxy_path_arg``
    seam — the single source of truth for the VS Code-proxy re-encode fix,
    used by every route that reads a filesystem path from the query string.
    """
    from lib.request_parser import decode_proxy_path_arg

    return decode_proxy_path_arg(name)


def _project_user_id() -> int:
    """Resolve the authenticated owner at the HTTP boundary."""
    return int(_request_user_id())


# ── State / lifecycle ────────────────────────────────────────────────


@api_v1_project_bp.route("/api/v1/project/set", methods=["POST"])
@require_auth
@api_meta(
    summary="Set the primary project root",
    description="Replaces any existing primary path; clears extras.",
    tags=["project"],
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["path"],
                    "properties": {"path": {"type": "string"}},
                }
            }
        },
    },
)
def project_set():
    data = parse_body(exact=('path',))
    path = data.get("path", "").strip()
    if not path:
        return api_bad_request("No path provided", field="path")
    try:
        from lib.project_mod import set_project

        return api_ok({**set_project(path)})
    except Exception as e:
        logger.error("[Project.v1] set failed for path %s: %s", path, e, exc_info=True)
        return api_bad_request(e)


@api_v1_project_bp.route("/api/v1/project/paths", methods=["PUT"])
@require_auth
@api_meta(
    summary="Atomically set primary + extra project paths",
    description=(
        "Body: ``{paths, readOnlyPaths?, recentPaths?}``. ``recentPaths`` is "
        "an optional bounded subset touched only after path validation and "
        "successful reconciliation. An exact primary/root/access "
        "reconciliation is otherwise side-effect-free."
    ),
    tags=["project"],
)
def project_paths():
    data = parse_body(exact=("paths", "readOnlyPaths", "recentPaths"))
    paths = data.get("paths", [])
    if not paths or not isinstance(paths, list):
        return api_error(
            'Provide a "paths" array with at least one directory', status=400
        )
    readonly = data.get("readOnlyPaths") or []
    if not isinstance(readonly, list):
        readonly = []
    try:
        recent_paths = _project_set_recent_paths(data, paths)
    except ValueError as exc:
        return api_bad_request(str(exc), field="recentPaths")
    try:
        from lib.project_mod import save_recent_projects, set_project_paths

        result = set_project_paths(paths, readonly_paths=readonly)
        if recent_paths:
            try:
                save_recent_projects(
                    recent_paths,
                    user_id=int(_request_user_id()),
                )
            except Exception as exc:
                # Recent navigation is reconstructible and was previously a
                # fire-and-forget second request. It cannot roll back a valid
                # project selection when its optional persistence is down.
                logger.warning(
                    "[Project.v1] recent-path batch skipped after set: %s",
                    exc,
                )
        return api_ok({**result})
    except Exception as e:
        logger.error("[Project.v1] paths failed for %s: %s", paths, e, exc_info=True)
        return api_bad_request(e)


@api_v1_project_bp.route("/api/v1/project/git-root-hint", methods=["POST"])
@require_auth
@api_meta(
    summary="Nearest enclosing git root for a directory",
    description=(
        "Body: ``{path}``. Returns ``{path, gitRoot}`` where ``gitRoot`` is "
        "the nearest ancestor (inclusive) containing a ``.git`` marker, or "
        "``null``. The project modal uses it to suggest the real repo root "
        "when the user picks a subdirectory."
    ),
    tags=["project"],
)
def project_git_root_hint():
    data = parse_body(exact=('path',))
    path = (data.get("path") or "").strip()
    if not path:
        return api_bad_request("No path provided", field="path")
    abs_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(abs_path):
        return api_bad_request(
            f"Directory not found: {abs_path}", field="path")
    from lib.project_mod import find_git_root

    return api_ok({"path": abs_path, "gitRoot": find_git_root(abs_path)})


@api_v1_project_bp.route("/api/v1/project/status", methods=["GET"])
@require_auth
@api_meta(summary="Active project state", tags=["project"])
def project_status():
    # Conv-scoped bar: when the client passes ?conv_id=<id>, source the
    #   project state from THAT conversation's own scoped registry rather than
    #   the process-global _roots. This stops a background task's absolute-path
    #   write (which registers into the global registry) from bleeding extra
    #   paths onto a different conversation's project bar. Without conv_id the
    #   legacy global view is returned (byte-identical to before).
    conv_id = (request.args.get("conv_id") or "").strip()
    if conv_id:
        from lib.project_mod import get_state_for_conv

        return api_ok(get_state_for_conv(
            conv_id, _conversation_saved_roots(conv_id, _request_user_id())))
    from lib.project_mod import get_state

    return api_ok(get_state())


def _conversation_saved_roots(conv_id, user_id):
    """The conversation's own durable workspace roots, primary first.

    The per-conversation root registry in ``lib.project_mod.config`` is an
    EPHEMERAL execution cache: it stays cold until a turn registers roots, so
    a conversation reopened after a restart has no registry at all. Its saved
    settings are then the only authority for which root the bar describes, and
    every enrichment the bar renders (notably the git branch) is probed from
    that root — never from the process-global registry, which may hold another
    conversation's project.
    """
    from lib.conversations.repository import ConversationRepository

    snapshot = ConversationRepository().get(
        conv_id, user_id=user_id, include_messages=False)
    settings = snapshot.metadata.get('settings') if snapshot else None
    if not isinstance(settings, dict):
        return ()
    paths = settings.get('projectPaths')
    ordered = [settings.get('projectPath')]
    if isinstance(paths, list):
        ordered.extend(paths)
    roots = []
    for path in ordered:
        if isinstance(path, str) and path.strip() and path not in roots:
            roots.append(path)
    return tuple(roots)


@api_v1_project_bp.route("/api/v1/project", methods=["DELETE"])
@require_auth
@api_meta(summary="Clear the active project", tags=["project"])
def project_clear():
    from lib.project_mod import clear_project

    clear_project()
    return api_ok()


@api_v1_project_bp.route("/api/v1/project/browse", methods=["POST"])
@require_auth
@api_meta(
    summary="List a directory",
    description="Body: ``{path?, showHidden?}``. Used by the file picker.",
    tags=["project"],
    request_body={'content': {'application/json': {'schema': browse_request_schema()}}},
)
def project_browse():
    data = parse_body(strict=True, exact=tuple(browse_request_schema()["properties"]))
    try:
        path, show_hidden = normalize_browse_request(data)
    except ValueError as error:
        return api_bad_request(str(error))
    from lib.project_mod import browse_directory

    result = browse_directory(path, show_hidden=show_hidden)
    if result.get("error"):
        return api_payload(result, 400)
    return api_ok(result)


@api_v1_project_bp.route("/api/v1/project/mkdir", methods=["POST"])
@require_auth
@api_meta(
    summary="Create a new folder",
    description=(
        "Body: ``{parent, name}``. Creates ``name`` (a single "
        "non-navigating segment) under the existing ``parent`` "
        'directory. Used by the file picker\'s "New folder" action.'
    ),
    tags=["project"],
)
def project_mkdir():
    data = parse_body(exact=('name', 'parent',))
    parent = (data.get("parent") or "").strip()
    name = (data.get("name") or "").strip()
    if not parent:
        return api_bad_request("parent is required", field="parent")
    if not name:
        return api_bad_request("name is required", field="name")
    from lib.project_mod import create_directory

    result = create_directory(parent, name)
    if result.get("error"):
        return api_payload(result, 400)
    return api_ok(result)


@api_v1_project_bp.route("/api/v1/project/rmdir", methods=["POST"])
@require_auth
@api_meta(
    summary="Delete a folder (moved to a recoverable trash bin)",
    description=(
        "Body: ``{path}``. Moves the directory into a ``.tofu_trash`` "
        "bin rather than an irreversible delete. Refuses system "
        "paths and active workspace roots. Used by the file picker's "
        '"Delete folder" action.'
    ),
    tags=["project"],
)
def project_rmdir():
    data = parse_body(exact=('path',))
    path = (data.get("path") or "").strip()
    if not path:
        return api_bad_request("path is required", field="path")
    from lib.project_mod import delete_directory

    result = delete_directory(path)
    if result.get("error"):
        return api_payload(result, 400)
    return api_ok(result)


# ── Recent projects ──────────────────────────────────────────────────

# Bound the per-entry existence probe: on a cold network filesystem each
# isdir can take hundreds of milliseconds, so a sequential loop multiplied
# that latency by the list size and stalled the project modal after every
# server restart. Concurrent stats bound the wait by the slowest single
# probe; the overall deadline keeps a hung mount from pinning the request.
_RECENT_EXISTS_WORKERS = 4
_RECENT_EXISTS_DEADLINE_S = 2.0


def _annotate_recent_exists(projects: list) -> None:
    """Stamp ``exists`` on each recent-project row for rename detection.

    A stored path that no longer resolves is surfaced (never silently
    dropped) so the modal can badge it and offer relink. A probe that does
    not answer within the deadline stays optimistic: the path was a
    directory when recorded, and a wrong ``True`` only defers rename
    detection to the browse call — a wrong ``False`` would offer relink
    for a directory that is merely slow to stat.
    """
    pool = ThreadPoolExecutor(
        max_workers=max(1, min(_RECENT_EXISTS_WORKERS, len(projects))),
        thread_name_prefix="recent-exists")
    futures = {}
    try:
        for item in projects:
            path = str(item.get("path") or "")
            if path:
                futures[pool.submit(
                    os.path.isdir, os.path.expanduser(path))] = item
            else:
                item["exists"] = False
        try:
            for future in as_completed(
                    futures, timeout=_RECENT_EXISTS_DEADLINE_S):
                # os.path.isdir already swallows OSError into False.
                futures[future]["exists"] = bool(future.result())
        except FuturesTimeoutError:
            pass
        for item in projects:
            item.setdefault("exists", True)
    finally:
        # A still-hung FUSE stat must not hold the response (shutdown
        # wait=True would); the bounded worker set retires on its own.
        pool.shutdown(wait=False, cancel_futures=True)


@api_v1_project_bp.route("/api/v1/project/recent", methods=["GET", "POST", "DELETE"])
@require_auth
@api_meta(
    summary="Recent project paths CRUD",
    description="GET → list, POST {path} → add, DELETE → wipe.",
    tags=["project"],
)
def project_recent():
    from lib.project_mod import (
        clear_recent_projects,
        get_recent_projects,
        save_recent_project,
    )

    from routes.api_v1.auth import request_user_id

    owner_user_id = int(request_user_id())
    if request.method == "POST":
        data = parse_body(exact=('path',))
        path = data.get("path", "").strip()
        if path:
            save_recent_project(path, user_id=owner_user_id)
        return api_ok()
    if request.method == "DELETE":
        clear_recent_projects(user_id=owner_user_id)
        return api_ok()
    projects = get_recent_projects(user_id=owner_user_id)
    _annotate_recent_exists(projects)
    return api_ok({"projects": projects})


@api_v1_project_bp.route(
    "/api/v1/project/recent/relink", methods=["POST"])
@require_auth
@api_meta(
    summary="Re-key a renamed/moved project to its new path",
    description=(
        "Body: ``{oldPath, newPath}``. ``newPath`` must be an existing "
        "directory. Moves the recent entry (merging an existing one), active "
        "and recoverable conversation project pins, Project Brain "
        "projection, and its retained event tail."
    ),
    tags=["project"],
)
def project_recent_relink():
    data = parse_body(exact=('newPath', 'oldPath',))
    # oldPath is matched verbatim against the stored recent key — only
    # newPath is normalized (it must resolve on disk).
    old_path = (data.get("oldPath") or "").strip()
    new_path = (data.get("newPath") or "").strip()
    if not old_path:
        return api_bad_request("No oldPath provided", field="oldPath")
    if not new_path:
        return api_bad_request("No newPath provided", field="newPath")
    abs_new = os.path.abspath(os.path.expanduser(new_path))
    if not os.path.isdir(abs_new):
        return api_bad_request(
            f"Directory not found: {abs_new}", field="newPath")
    if old_path == abs_new:
        return api_bad_request(
            "oldPath and newPath are identical", field="newPath")
    from lib.project_mod import relink_project_path
    from lib.storage.errors import StorageError

    try:
        result = relink_project_path(
            old_path, abs_new, user_id=int(_request_user_id()))
    except StorageError as exc:
        if exc.code == "database_not_found":
            return api_not_found("Old path is not in recent projects")
        raise
    return api_ok(result)


# ── Approval / undo / redo / rescan ─────────────────────────────────


@api_v1_project_bp.route("/api/v1/project/write-approval", methods=["POST"])
@require_auth
@api_meta(
    summary="Resolve a pending write-approval prompt",
    description="Body: ``{approvalId, approved}``.",
    tags=["project"],
)
def project_write_approval():
    data = parse_body(exact=('approvalId', 'approved',))
    approval_id = optional_str(
        data, "approvalId", default="",
        max_len=MAX_HUMAN_GATE_REQUEST_ID_LENGTH)
    approved = optional_bool(data, "approved", default=False)
    if not approval_id:
        return api_bad_request("No approvalId", field="approvalId")
    from lib.tasks_pkg.approval import resolve_write_approval

    if not resolve_write_approval(
        approval_id,
        approved,
        owner_user_id=_request_user_id(),
    ):
        return api_not_found("Approval not found or expired")
    return api_ok({"approved": approved})


@api_v1_project_bp.route("/api/v1/project/undo", methods=["POST"])
@require_auth
@api_meta(
    summary="Undo file modifications",
    description="Body: ``{taskId|convId, projectPath?}``. ``taskId`` undoes only that round; "
    "``convId`` undoes the entire conversation's changes.",
    tags=["project"],
)
def project_undo():
    data = parse_body(exact=('convId', 'projectPath', 'taskId',))
    task_id = data.get("taskId", "").strip()
    conv_id = data.get("convId", "").strip()
    explicit_path = data.get("projectPath", "").strip()
    # Concurrency-safe resolution: an explicit projectPath (sent by the
    #   frontend per-conversation) wins. Otherwise recover the project that
    #   actually recorded this task/conv — NEVER fall back to the globally-
    #   active project (_state['path']), which may point at a different
    #   project when conversations edit projects concurrently, causing undo
    #   to silently no-op (undone=0).
    if not explicit_path:
        from lib.project_mod import resolve_base_path

        explicit_path = (
            resolve_base_path(task_id=task_id or None, conv_id=conv_id or None) or ""
        )
    project_path = _active_project_path(explicit_path)
    if not project_path:
        return api_bad_request("No active project")

    try:
        if task_id:
            from lib.project_mod import undo_task_modifications

            result = undo_task_modifications(project_path, task_id)
            logger.info(
                "[Project.v1] undo task=%s: undone=%s failed=%s",
                task_id[:8],
                result.get("undone", 0),
                result.get("failed", 0),
            )
        elif conv_id:
            from lib.project_mod import undo_conv_modifications

            result = undo_conv_modifications(project_path, conv_id)
            logger.info(
                "[Project.v1] undo conv=%s: undone=%s failed=%s",
                conv_id[:8],
                result.get("undone", 0),
                result.get("failed", 0),
            )
        else:
            return api_bad_request("Provide taskId or convId")
        return api_ok(result)
    except Exception as e:
        logger.error("[Project.v1] undo failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.undo")


@api_v1_project_bp.route("/api/v1/project/undo-all", methods=["POST"])
@require_auth
@api_meta(
    summary="Undo every pending file modification in ONE project",
    description=(
        "Body: ``{projectPath?}``. Reverts all pending modifications recorded "
        "for a SINGLE project (across every conversation that edited it) — "
        "NOT a global wipe across all projects. The target project is the "
        "explicit ``projectPath`` (pinned per-conversation by the frontend) "
        "and falls back to the UI-active project only when omitted."
    ),
    tags=["project"],
)
def project_undo_all():
    data = parse_body(exact=('projectPath',))
    project_path = _active_project_path(data.get("projectPath", "").strip())
    if not project_path:
        return api_bad_request("No active project")
    try:
        from lib.project_mod import undo_all_modifications

        result = undo_all_modifications(project_path)
        logger.info(
            "[Project.v1] undo_all: undone=%s failed=%s",
            result.get("undone", 0),
            result.get("failed", 0),
        )
        return api_ok(result)
    except Exception as e:
        logger.error("[Project.v1] undo_all failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.undo_all")


@api_v1_project_bp.route("/api/v1/project/redo", methods=["POST"])
@require_auth
@api_meta(
    summary="Re-apply a previously-undone round",
    description="Body: ``{taskId, projectPath?}``.",
    tags=["project"],
)
def project_redo():
    data = parse_body(exact=('projectPath', 'taskId',))
    task_id = (data.get("taskId") or "").strip()
    if not task_id:
        return api_bad_request("taskId is required", field="taskId")
    # Concurrency-safe resolution, mirroring project_undo: an explicit
    #   projectPath (pinned per-conversation by the frontend) wins; otherwise
    #   recover the project that actually recorded this task from its snapshot
    #   — NEVER fall back to the globally-active project (_state['path']),
    #   which may point at a different project when conversations edit
    #   projects concurrently, sending redo to the wrong project.
    explicit_path = (data.get("projectPath") or "").strip()
    if not explicit_path:
        from lib.project_mod import resolve_base_path

        explicit_path = resolve_base_path(task_id=task_id) or ""
    project_path = _active_project_path(explicit_path)
    if not project_path:
        return api_bad_request("No active project")
    try:
        from lib.project_mod import redo_task_modifications

        result = redo_task_modifications(project_path, task_id)
        logger.info(
            "[Project.v1] redo task=%s: redone=%s ok=%s",
            task_id[:8],
            result.get("redone", 0),
            result.get("ok"),
        )
        return api_ok(result)
    except Exception as e:
        logger.error("[Project.v1] redo failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.redo")


@api_v1_project_bp.route("/api/v1/project/rescan", methods=["POST"])
@require_auth
@api_meta(
    summary="Re-scan the active project (refresh file index + stats)", tags=["project"]
)
def project_rescan():
    try:
        from lib.project_mod import rescan

        return api_ok(rescan() or {})
    except Exception as e:
        logger.error("[Project.v1] rescan failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.rescan")


# ── .gitignore suggestions ──────────────────────────────────────────


@api_v1_project_bp.route("/api/v1/project/gitignore/suggestions", methods=["GET"])
@require_auth
@api_meta(
    summary="List pending .gitignore suggestions",
    description="Detected from grep timeouts on dirs with no source files.",
    tags=["project"],
)
def project_gitignore_suggestions():
    # GET route → the projectPath query arg can be proxy-double-encoded, same
    # as the project-brain reads. Decode-until-stable before resolving.
    project_path = _active_project_path(_decoded_path_arg("projectPath"))
    if not project_path:
        return api_bad_request("No active project")
    try:
        from lib.project_mod.gitignore_suggest import get_suggestions

        return api_ok(
            {"projectPath": project_path, "suggestions": get_suggestions(project_path)}
        )
    except Exception as e:
        logger.error("[Project.v1] gitignore/suggestions failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.gitignore_suggestions")


@api_v1_project_bp.route("/api/v1/project/gitignore/accept", methods=["POST"])
@require_auth
@api_meta(
    summary="Append directories to the project .gitignore",
    description=(
        "Only dirs currently in the suggestion registry are accepted "
        "(defense against arbitrary writes). Existing entries are skipped."
    ),
    tags=["project"],
)
def project_gitignore_accept():
    data = parse_body(exact=('dirs', 'projectPath',))
    project_path = _active_project_path((data.get("projectPath") or "").strip())
    dirs = data.get("dirs") or []
    if not project_path:
        return api_bad_request("No active project")
    if not isinstance(dirs, list) or not dirs:
        return api_bad_request("dirs must be a non-empty list", field="dirs")
    try:
        from lib.project_mod.gitignore_suggest import accept_suggestions

        result = accept_suggestions(project_path, dirs)
        if "error" in result:
            return api_payload(result, 400)
        logger.info(
            "[Project.v1] gitignore/accept %s: added=%s skipped=%s unknown=%s",
            project_path,
            result.get("added"),
            result.get("skipped_existing"),
            result.get("unknown"),
        )
        return api_ok({**result})
    except Exception as e:
        logger.error("[Project.v1] gitignore/accept failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.gitignore_accept")


@api_v1_project_bp.route("/api/v1/project/gitignore/dismiss", methods=["POST"])
@require_auth
@api_meta(
    summary="Drop dirs from the suggestion registry",
    description="No .gitignore write \u2014 just removes from the in-memory list.",
    tags=["project"],
)
def project_gitignore_dismiss():
    data = parse_body(exact=('dirs', 'projectPath',))
    project_path = _active_project_path((data.get("projectPath") or "").strip())
    dirs = data.get("dirs") or []
    if not project_path:
        return api_bad_request("No active project")
    if not isinstance(dirs, list) or not dirs:
        return api_bad_request("dirs must be a non-empty list", field="dirs")
    try:
        from lib.project_mod.gitignore_suggest import dismiss_suggestions

        return api_ok({"removed": dismiss_suggestions(project_path, dirs)})
    except Exception as e:
        logger.error("[Project.v1] gitignore/dismiss failed: %s", e, exc_info=True)
        return api_internal_error(e, source="api_v1.project.gitignore_dismiss")


# ── Direct file write (Apply Code button) ────────────────────────────


@api_v1_project_bp.route("/api/v1/project/write", methods=["POST"])
@require_auth
@api_meta(
    summary="Write content to a project file",
    description=(
        "Writes ``content`` to the project-relative ``path``, creating "
        'parent directories as needed. Used by the "Apply Code" button '
        "in the code-block hover overlay. Returns "
        "``{ok, path, created, lines}``."
    ),
    tags=["project"],
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                }
            }
        },
    },
)
def project_write():
    data = parse_body(exact=('content', 'path',))
    path = (data.get("path") or "").strip()
    content = data.get("content", "")
    if not path:
        return api_bad_request("path is required", field="path")

    project_path = _active_project_path()
    if not project_path:
        return api_bad_request("No active project")

    from lib.project_mod.write_tools import tool_write_file

    result = tool_write_file(project_path, path, content)
    if not result.get("ok"):
        logger.warning(
            "[Project.v1] write failed for %s: %s", path, result.get("error")
        )
        return api_payload(result, 400)
    return api_ok(result)


@api_v1_project_bp.route("/api/v1/project/upload", methods=["POST"])
@require_auth
@api_meta(
    summary="Save a dropped file into a project folder (binary-safe)",
    description=(
        "multipart/form-data: ``file`` (the raw bytes) + ``dir`` (the "
        "destination directory — absolute path inside an already-attached "
        "workspace root, or empty for the active project root) + optional "
        "``name`` override. Unlike ``/write`` (text only), this preserves "
        "raw bytes so images / PDFs / archives land intact. The destination "
        "must be inside an attached root (a drop never auto-registers a new "
        "one); read-only roots are refused; name collisions auto-rename. The "
        "write is recorded so it appears in the file-changes bar and is "
        "undoable. Backs drag-and-drop onto the folder browser."
    ),
    tags=["project"],
)
def project_upload():
    form = request_form()
    dest_dir = (form.get("dir") or "").strip()
    name_override = (form.get("name") or "").strip()

    files = request_files()
    if "file" not in files:
        return api_bad_request("No file", field="file")
    upload = files["file"]
    fname = name_override or (upload.filename or "")
    # Reject navigation in the client-supplied filename — a drop names a leaf,
    # never a path. os.path.basename strips any directory the browser attached.
    fname = os.path.basename(fname.replace("\\", "/")).strip()
    if not fname or fname in (".", ".."):
        return api_bad_request("No filename", field="file")

    # Receive complete source bytes in an owned temp file before the existing
    # project write boundary materializes its input.
    from lib.paper.archive import stream_to_temp
    from lib.paper.builds.errors import BuildError
    spooled = None
    try:
        spooled = stream_to_temp(upload.stream)
        data = spooled.read_bytes()
    except BuildError as e:
        return api_payload({'ok': False, 'error': str(e), 'kind': e.code}, 400)
    except Exception as e:
        logger.error("[Project.v1] upload stream read failed: %s", e, exc_info=True)
        return api_internal_error("internal_error")
    finally:
        if spooled is not None:
            spooled.unlink(missing_ok=True)

    # Destination directory: an explicit dir (must be inside an attached root,
    # enforced by save_uploaded_file) or the active project root.
    project_path = _active_project_path()
    if dest_dir:
        target_path = os.path.join(os.path.abspath(os.path.expanduser(dest_dir)), fname)
    else:
        if not project_path:
            return api_bad_request("No active project")
        target_path = fname  # project-relative → active root

    from lib.project_mod.write_tools import save_uploaded_file

    result = save_uploaded_file(project_path or dest_dir, target_path, data)
    if not result.get("ok"):
        logger.warning(
            "[Project.v1] upload failed for %s: %s", target_path, result.get("error")
        )
        return api_payload(result, 400)
    logger.info(
        "[Project.v1] upload saved %s (%d bytes, renamed=%s)",
        result.get("path"),
        result.get("bytesWritten", 0),
        result.get("renamed"),
    )
    return api_ok(result)


def _attached_root_for(path: str) -> str:
    """Return the registered workspace root containing *path*, or ''."""
    from lib.project_mod.config import _lock, _roots
    norm = os.path.abspath(os.path.expanduser(path))
    with _lock:
        roots = [rs['path'] for rs in _roots.values()]
    for root in roots:
        base = os.path.abspath(root).rstrip(os.sep) or root
        if norm == base or norm.startswith(base + os.sep):
            return base
    return ''


@api_v1_project_bp.route("/api/v1/project/upload-archive", methods=["POST"])
@require_auth
@api_meta(
    summary="Extract a dropped .zip/.tar.gz project archive into a folder",
    description=(
        "multipart/form-data: ``file`` (the archive) + ``dir`` (destination "
        "directory inside an already-attached workspace root, or empty for "
        "the active project root). The archive is spooled to an owned temp "
        "file, extracted atomically into ``<dir>/<archive-stem>/`` (fresh "
        "auto-suffixed directory; existing files are never overwritten), "
        "with zip-slip/link defense and complete regular-file reception. "
        "The response lists the extracted "
        "tree plus TeX main-document candidates so the client can offer a "
        "paper preview immediately. Backs drag-and-drop of arXiv/Overleaf "
        "source exports onto the folder browser."
    ),
    tags=["project"],
)
def project_upload_archive():
    from lib.paper.archive import extract_archive, stream_to_temp
    from lib.paper.builds.errors import BuildError

    form = request_form()
    dest_dir = (form.get("dir") or "").strip()
    files = request_files()
    if "file" not in files:
        return api_bad_request("No file", field="file")
    upload = files["file"]
    archive_name = os.path.basename((upload.filename or "archive").replace("\\", "/"))

    project_path = _active_project_path()
    if dest_dir:
        parent = os.path.abspath(os.path.expanduser(dest_dir))
    elif project_path:
        parent = os.path.abspath(project_path)
    else:
        return api_bad_request("No active project")
    root = _attached_root_for(parent)
    if not root:
        return api_payload({'ok': False, 'errorKind': 'path-invalid',
                            'error': ('Destination is not inside any attached workspace '
                                      'folder. Add it as a project folder first, then drop.')}, 400)
    try:
        from lib.project_mod.write_tools import _enforce_not_readonly
        _enforce_not_readonly(parent)
    except ValueError as e:
        return api_payload({'ok': False, 'errorKind': 'path-invalid', 'error': str(e)}, 400)

    spooled = None
    try:
        spooled = stream_to_temp(upload.stream)
        receipt = extract_archive(spooled, parent, name_hint=archive_name)
    except BuildError as e:
        kind = {'archive_limit': 'archive-too-large', 'archive_invalid': 'archive-invalid',
                'archive_empty': 'archive-empty', 'archive_path': 'archive-invalid'}.get(
                    e.code, 'archive-invalid')
        return api_payload({'ok': False, 'errorKind': kind, 'error': str(e)}, 400)
    except OSError as e:
        logger.warning("[Project.v1] archive extraction failed: %s", e, exc_info=True)
        return api_payload({'ok': False, 'error': 'Cannot store the extracted archive'}, 500)
    finally:
        if spooled is not None:
            spooled.unlink(missing_ok=True)

    tex_candidates = []
    try:
        for candidate in sorted(receipt.directory.rglob('*.tex'))[:64]:
            try:
                head = candidate.read_text(errors='replace')[:8192]
            except OSError:
                continue
            if '\\documentclass' in head:
                tex_candidates.append(
                    candidate.relative_to(receipt.directory).as_posix())
    except OSError:
        pass
    logger.info("[Project.v1] archive extracted %s (%d files, %d bytes, %d tex candidates)",
                receipt.directory, receipt.files, receipt.bytes_written, len(tex_candidates))
    result = receipt.as_dict(parent)
    result.update(ok=True, action='upload_archive', root=root,
                  texMainCandidates=tex_candidates[:16])
    return api_ok(result)


__all__ = ["api_v1_project_bp"]
