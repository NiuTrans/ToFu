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

All require ``@require_auth``. Mutations that change ``data/config/`` or
walk the filesystem keep a ``rate_limit`` circuit-breaker against runaway
client retry loops.  It is sized at 120/min (sustained 2 rps): the file
picker's own legitimate bursts — per-conversation restore on every conv
switch, RO re-hydrate after a server restart — can exceed 10/min, so a
tighter ceiling only ever bites the owner.  ``TOFU_RATE_LIMIT=off``
disables the buckets entirely on single-user installs.

TODO(enterprise, I7): project selection, board, and charter state are
instance-global behind bare ``@require_auth`` — any authenticated user can
retarget the shared project root. Key all state by ``auth_ctx.user_id``.
docs/ENTERPRISE_READINESS_AUDIT.md
"""

from __future__ import annotations

import os

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
from lib.project_recent_contract import (
    RECENT_PROJECT_PATH_MAX_CHARS,
    RECENT_PROJECT_TOUCH_BATCH_LIMIT,
)
from lib.rate_limiter import rate_limit
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
@rate_limit(limit=120, per=60)
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
    data = parse_body()
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
@rate_limit(limit=120, per=60)
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
    data = parse_body()
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
@rate_limit(limit=120, per=60)
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
    data = parse_body()
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

        return api_ok(get_state_for_conv(conv_id))
    from lib.project_mod import get_state

    return api_ok(get_state())


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
)
def project_browse():
    data = parse_body()
    path = data.get("path", "").strip() or None
    show_hidden = data.get("showHidden", False)
    from lib.project_mod import browse_directory

    result = browse_directory(path, show_hidden=show_hidden)
    if result.get("error"):
        return api_payload(result, 400)
    return api_ok(result)


@api_v1_project_bp.route("/api/v1/project/mkdir", methods=["POST"])
@require_auth
@rate_limit(limit=120, per=60)
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
    data = parse_body()
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
@rate_limit(limit=120, per=60)
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
    data = parse_body()
    path = (data.get("path") or "").strip()
    if not path:
        return api_bad_request("path is required", field="path")
    from lib.project_mod import delete_directory

    result = delete_directory(path)
    if result.get("error"):
        return api_payload(result, 400)
    return api_ok(result)


# ── Recent projects ──────────────────────────────────────────────────


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
        data = parse_body()
        path = data.get("path", "").strip()
        if path:
            save_recent_project(path, user_id=owner_user_id)
        return api_ok()
    if request.method == "DELETE":
        clear_recent_projects(user_id=owner_user_id)
        return api_ok()
    projects = get_recent_projects(user_id=owner_user_id)
    # Rename detection: a stored path that no longer resolves is surfaced
    # (never silently dropped) so the modal can badge it and offer relink.
    for item in projects:
        item_path = item.get("path") or ""
        item["exists"] = bool(item_path) and os.path.isdir(
            os.path.expanduser(item_path))
    return api_ok({"projects": projects})


@api_v1_project_bp.route(
    "/api/v1/project/recent/relink", methods=["POST"])
@require_auth
@rate_limit(limit=120, per=60)
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
    data = parse_body()
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
    data = parse_body()
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
    data = parse_body()
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
    data = parse_body()
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
    data = parse_body()
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
@rate_limit(limit=10, per=60)
@api_meta(
    summary="Append directories to the project .gitignore",
    description=(
        "Only dirs currently in the suggestion registry are accepted "
        "(defense against arbitrary writes). Existing entries are skipped."
    ),
    tags=["project"],
)
def project_gitignore_accept():
    data = parse_body()
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
    data = parse_body()
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
@rate_limit(limit=20, per=60)
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
    data = parse_body()
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
@rate_limit(limit=20, per=60)
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

    try:
        data = upload.stream.read()
    except Exception as e:
        logger.error("[Project.v1] upload stream read failed: %s", e, exc_info=True)
        return api_internal_error("internal_error")

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


__all__ = ["api_v1_project_bp"]
