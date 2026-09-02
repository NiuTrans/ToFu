# HOT_PATH
"""Cross-conversation handlers for references, charter, board and peers."""

from __future__ import annotations

import re

from lib.conv_ref import execute_conv_ref_tool
from lib.log import get_logger
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call
from lib.tasks_pkg.manager import append_event
from lib.tools.conversation import (
    BOARD_TOOL_NAMES,
    CHARTER_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    INTEGRATION_TOOL_NAMES,
    PEER_TOOL_NAMES,
)

logger = get_logger(__name__)


@tool_registry.tool_set(
    CONV_REF_TOOL_NAMES,
    category="conversations",
    description="List and retrieve past conversations",
)
def _handle_conv_ref_tool(
    task,
    tc,
    fn_name,
    tc_id,
    fn_args,
    rn,
    round_entry,
    cfg,
    project_path,
    project_enabled,
    all_tools=None,
):
    current_conv_id = task.get("convId")
    # Multi-tenant scoping: conv_ref reads the conversations table, so it must
    # be scoped to the principal who owns THIS task. Background task threads
    # have no request context, so the owner comes from task['_userId'] via the
    # canonical helper used for notifications and every durable task access.
    from lib.tasks_pkg.manager._registry import task_user_id

    _user_id = task_user_id(task)

    def _run(_fn_name, _fn_args):
        return execute_conv_ref_tool(
            _fn_name,
            _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path,
            user_id=_user_id,
        )

    detail = (
        fn_args.get("keyword", "all")
        if fn_name == "list_conversations"
        else fn_args.get("conversation_id", "?")[:8]
    )

    def _post_build(meta, _tool_content, _fn_args):
        """Attach a STRUCTURED conversation digest for get_conversation so the
        frontend renders a clean human-view card instead of dumping the raw
        transcript. Read off the DB (never re-parsed from the prose/raw result);
        the verbatim record stays available via the row's "model view"
        affordance.

        Applies to raw-mode reads TOO (2026-07-23): the raw ``═══ Raw
        Conversation Record`` + JSON dump is tens of KB and tripped the L0
        "Output too large → persist → preview" path, so a raw read rendered as
        an ugly truncated JSON blob with NO card. The digest is rebuilt
        INDEPENDENTLY off the DB row, so raw mode now gets the same human card;
        the full JSON stays available on the model channel."""
        if fn_name != "get_conversation":
            return
        conv_id = (_fn_args.get("conversation_id") or "").strip()
        if not conv_id:
            return
        try:
            from lib.conv_ref import build_conversation_digest, raw_requested

            digest = build_conversation_digest(
                conv_id,
                current_conv_id=current_conv_id,
                raw=raw_requested(_fn_args),
                user_id=_user_id,
            )
        except Exception as e:
            logger.debug("[ConvRef] digest build failed for %s: %s", conv_id, e)
            return
        if digest:
            meta["convDigest"] = digest

    return simple_call(
        task,
        fn_name,
        fn_args,
        rn,
        round_entry,
        tc_id,
        executor=_run,
        source="Conversations",
        module_tag="ConvRef",
        title=f"{fn_name}: {detail}",
        post_build=_post_build,
    )


@tool_registry.tool_set(
    CHARTER_TOOL_NAMES,
    category="conversations",
    description="Read / propose / commit project charter decisions (north star)",
)
def _handle_charter_tool(
    task,
    tc,
    fn_name,
    tc_id,
    fn_args,
    rn,
    round_entry,
    cfg,
    project_path,
    project_enabled,
    all_tools=None,
):
    current_conv_id = task.get("convId", "")
    from lib.tasks_pkg.manager._registry import task_user_id

    owner_user_id = int(task_user_id(task))

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_charter import execute_charter_tool

        return execute_charter_tool(
            _fn_name,
            _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else "",
            user_id=owner_user_id,
        )

    verb = {
        "project_charter_read": "read",
        "project_charter_propose": "propose",
        "project_charter_commit": "commit",
    }.get(fn_name, "charter")
    # Structured enrichment (rendered off engine/args data, NOT re-parsed prose):
    # a propose carries the proposal text + a pending-human-review marker so the
    # frontend can render a distinct "awaiting review" affordance.
    _extra = None
    if fn_name == "project_charter_propose":
        _extra = {
            "charterProposal": {
                "proposal": (fn_args.get("proposal") or "").strip(),
                "title": (fn_args.get("title") or "").strip(),
                "pending": True,
            }
        }
    return simple_call(
        task,
        fn_name,
        fn_args,
        rn,
        round_entry,
        tc_id,
        executor=_run,
        source="Charter",
        module_tag="Charter",
        badge=verb,
        extra=_extra,
    )


@tool_registry.tool_set(
    BOARD_TOOL_NAMES,
    category="conversations",
    description="Read / post / claim / complete / block project board epics",
)
def _handle_board_tool(
    task,
    tc,
    fn_name,
    tc_id,
    fn_args,
    rn,
    round_entry,
    cfg,
    project_path,
    project_enabled,
    all_tools=None,
):
    current_conv_id = task.get("convId", "")
    from lib.tasks_pkg.manager._registry import task_user_id

    owner_user_id = int(task_user_id(task))

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_board import execute_board_tool

        return execute_board_tool(
            _fn_name,
            _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else "",
            user_id=owner_user_id,
        )

    _verb = fn_name.replace("project_board_", "", 1)

    def _post_build(meta, _tool_content, _fn_args):
        """Attach a STRUCTURED board snapshot (read) or transition (mutation),
        read off the engine — never re-parsed from the prose result."""
        if not project_enabled or not project_path:
            return
        try:
            from lib.conversations.project_board import read_board

            board = read_board(project_path, user_id=owner_user_id)
        except Exception as e:
            logger.debug("[Board] post_build read failed: %s", e)
            return
        if fn_name == "project_board_read":
            # Compact mini-kanban: counts + lane epic titles (structured).
            lanes = {"open": [], "claimed": [], "done": []}
            for tk in board.get("tasks", []):
                lanes.setdefault(tk.get("status", "open"), []).append(
                    {
                        "id": tk.get("id", ""),
                        "title": tk.get("title", ""),
                        "owner": tk.get("owner_conv_id", ""),
                        "dispatched": bool(tk.get("dispatched")),
                    }
                )
            meta["boardSnapshot"] = {
                "open": board.get("open", 0),
                "claimed": board.get("claimed", 0),
                "done": board.get("done", 0),
                "lanes": lanes,
            }
        else:
            # Mutation → an explicit transition (verb + target epic + status).
            # A mutation can FAIL by RETURNING an error string (board full,
            # already-claimed, task-not-found) rather than raising — the old
            # transition meta looked byte-identical to a success (verb + title +
            # a GUESSED 'open' status), so a failed post/claim rendered as a
            # normal green card and the user only learned of the failure by
            # opening the raw model text (the reported bug). Detect the error
            # sentinel and carry ok/error onto the transition so the frontend
            # can render an explicit failed card.
            _content = (
                _tool_content if isinstance(_tool_content, str) else str(_tool_content)
            )
            _failed = _content.lstrip()[:40].startswith(
                ("Error", "❌", "NOT claimed", "Failed")
            )
            _err_msg = _content.strip() if _failed else ""
            # A POST carries NO task_id in its args (the id is minted
            # server-side); the epic title lives in the args and the freshly
            # posted epic is always 'open'. So a post is keyed off the args +
            # the id parsed from the result string ("Posted epic <id> …"),
            # NOT an args task_id lookup (which was empty → the card rendered a
            # bare verb with no title, the reported "shows nothing" bug).
            if fn_name == "project_board_post":
                tid = ""
                content = (
                    _tool_content
                    if isinstance(_tool_content, str)
                    else str(_tool_content)
                )
                m = re.search(r"\bpt_[0-9a-f]{6,}", content)
                if m:
                    tid = m.group(0)
                title = (_fn_args.get("title") or "").strip()
                status = "open"
                # Prefer the authoritative board row when the id resolves (it
                # carries the stored, length-capped title); fall back to args.
                if tid:
                    for tk in board.get("tasks", []):
                        if tk.get("id") == tid:
                            title = tk.get("title", "") or title
                            status = tk.get("status", "") or status
                            break
            else:
                tid = (_fn_args.get("task_id") or "").strip()
                title = ""
                status = ""
                for tk in board.get("tasks", []):
                    if tk.get("id") == tid:
                        title = tk.get("title", "")
                        status = tk.get("status", "")
                        break
            # A failed mutation posts/claims/etc NOTHING, so a guessed status
            # ('open') would be a lie — clear it and surface the error instead.
            if _failed:
                status = ""
            meta["boardTransition"] = {
                "verb": _verb,
                "taskId": tid,
                "title": title,
                "status": status,
                "ok": not _failed,
                "error": _err_msg,
            }

    return simple_call(
        task,
        fn_name,
        fn_args,
        rn,
        round_entry,
        tc_id,
        executor=_run,
        source="Board",
        module_tag="Board",
        badge=_verb,
        post_build=_post_build,
    )


@tool_registry.tool_set(
    INTEGRATION_TOOL_NAMES,
    category="conversations",
    description="Isolated-epic writer workspace: checkpoint / submit / status",
)
def _handle_integration_tool(
    task,
    tc,
    fn_name,
    tc_id,
    fn_args,
    rn,
    round_entry,
    cfg,
    project_path,
    project_enabled,
    all_tools=None,
):
    current_conv_id = task.get("convId", "")

    def _run(_fn_name, _fn_args):
        from lib.integration_control import execute_integration_tool
        from lib.tasks_pkg.manager import task_user_id

        return execute_integration_tool(
            _fn_name,
            _fn_args,
            project_path=project_path if project_enabled else "",
            user_id=int(task_user_id(task)),
            conv_id=current_conv_id,
        )

    _verb = fn_name.replace("integration_", "", 1)
    return simple_call(
        task,
        fn_name,
        fn_args,
        rn,
        round_entry,
        tc_id,
        executor=_run,
        source="Integration",
        module_tag="Integration",
        badge=_verb,
    )


def _make_intervention_approval_fn(task, rn, tc_id, round_entry):
    """Build the human-approval callback for a coercive peer hard-abort.

    Returns ``approval_fn(prompt) -> approver | None`` that routes the request
    through the SAME human-guidance seam ``ask_human`` uses: it emits a
    ``human_guidance_request`` choice event (Approve / Deny) the UI already
    renders + resolves, then BLOCKS on ``request_human_guidance`` until the
    human decides (or the task aborts). Grant → returns the approver identity
    (the resolving user, or 'human'); deny/abort → returns None.

    Under AUTOPILOT a coercive kill of another conversation is NEVER
    auto-authorized (the VU may freely answer questions, but must not silently
    green-light stopping a sibling) → returns None (advisory fallback).
    """
    from lib.ids import short_id

    def _approval_fn(prompt: str):
        from lib.tasks_pkg.autopilot import is_autopilot_enabled

        if is_autopilot_enabled(task):
            logger.info(
                "[Peer] hard-abort auto-DENIED under autopilot task=%s",
                task.get("id", "?")[:8],
            )
            return None
        guidance_id = short_id("hg_", 12)
        options = [
            {"label": "Approve abort", "value": "approve"},
            {"label": "Deny", "value": "deny"},
        ]
        round_entry["status"] = "awaiting_human"
        round_entry["guidanceId"] = guidance_id
        round_entry["guidanceQuestion"] = prompt
        round_entry["guidanceType"] = "choice"
        round_entry["guidanceOptions"] = options
        append_event(
            task,
            {
                "type": "human_guidance_request",
                "roundNum": rn,
                "toolCallId": tc_id,
                "guidanceId": guidance_id,
                "question": prompt,
                "responseType": "choice",
                "options": options,
                "intervention": True,
            },
        )
        from lib.tasks_pkg.human_guidance import request_human_guidance

        resp = request_human_guidance(guidance_id, task=task)
        if resp is None:
            return None  # task aborted while waiting
        rl = str(resp).strip().lower()
        approved = (
            ("approve" in rl or rl in ("yes", "ok", "y", "approved"))
            and not rl.startswith("deny")
            and rl not in ("no", "n")
        )
        if not approved:
            return None
        # Stamp the approver identity for the audit_log('intervention', …).
        who = str(resp).strip()
        return who if who and "approve" not in who.lower() else "human"

    return _approval_fn


@tool_registry.tool_set(
    PEER_TOOL_NAMES,
    category="conversations",
    description="Live peer status / peer messaging / advisory intervention",
)
def _handle_peer_tool(
    task,
    tc,
    fn_name,
    tc_id,
    fn_args,
    rn,
    round_entry,
    cfg,
    project_path,
    project_enabled,
    all_tools=None,
):
    current_conv_id = task.get("convId", "")
    from lib.tasks_pkg.manager._registry import task_user_id

    owner_user_id = int(task_user_id(task))

    # Only project_intervene(hard_abort=True) needs the human-approval seam;
    # build it lazily so status/message paths carry no approval overhead.
    approval_fn = None
    if fn_name == "project_intervene" and bool(fn_args.get("hard_abort")):
        approval_fn = _make_intervention_approval_fn(task, rn, tc_id, round_entry)

    def _run(_fn_name, _fn_args):
        from lib.conversations.project_peer import execute_peer_tool

        return execute_peer_tool(
            _fn_name,
            _fn_args,
            current_conv_id=current_conv_id,
            project_path=project_path if project_enabled else "",
            config=cfg,
            approval_fn=approval_fn,
            user_id=owner_user_id,
        )

    _verb = {
        "project_peer_status": "status",
        "project_feed_read": "feed",
        "project_message": "message",
        "project_intervene": "intervene",
    }.get(fn_name, "peer")

    def _post_build(meta, _tool_content, _fn_args):
        """Attach STRUCTURED meta (read off the engine, never re-parsed prose):
        the live peer list for ``project_peer_status``, the recent activity
        events for ``project_feed_read``, and a delivery descriptor for
        ``project_message`` / ``project_intervene``."""
        if not project_enabled or not project_path:
            return
        # ── project_peer_status → live peer cards ──
        if fn_name == "project_peer_status":
            try:
                from lib.conversations.project_peer import build_peer_status

                status = build_peer_status(
                    project_path, current_conv_id, user_id=owner_user_id
                )
            except Exception as e:
                logger.debug("[Peer] post_build status failed: %s", e)
                return
            target = (_fn_args.get("conv_id") or "").strip()
            peers = status.get("peers", [])
            if target:
                peers = [
                    p for p in peers if (p.get("convId", "") or "").startswith(target)
                ]
            meta["peerStatus"] = {
                "count": len(peers),
                "peers": [
                    {
                        "convId": p.get("convId", ""),
                        "agentId": p.get("agentId", ""),
                        "title": p.get("title", ""),
                        "statusLabel": p.get("statusLabel", ""),
                        "round": p.get("round", 0),
                        "currentFile": p.get("currentFile", ""),
                        "claimedEpic": p.get("claimedEpic", ""),
                    }
                    for p in peers
                ],
            }
            return
        # ── project_feed_read → chronological activity events ──
        if fn_name == "project_feed_read":
            try:
                limit = int(_fn_args.get("limit") or 25)
            except (TypeError, ValueError) as e:
                logger.debug(
                    "[Peer] feed_read limit=%r not an int (%s) — using default 25",
                    _fn_args.get("limit"),
                    e,
                )
                limit = 25
            limit = max(1, min(limit, 60))
            try:
                from lib.conversations.project_feed import read_project_feed

                feed = read_project_feed(
                    project_path, user_id=owner_user_id, limit=limit)
            except Exception as e:
                logger.debug("[Peer] post_build feed failed: %s", e)
                return
            events = feed.get("events", []) or []
            # Backfill a human-readable title for events whose stored title is
            # empty (task-lifecycle started/completed/aborted are emitted with
            # no title) so the card never shows a bare `conv <id>`. Same
            # DB-backed resolver build_peer_status uses (real title, else a
            # snippet of the opening user turn — never an id).
            need = list(
                {
                    ev.get("conv_id")
                    for ev in events
                    if not (ev.get("title") or "").strip() and ev.get("conv_id")
                }
            )
            titles = {}
            if need:
                try:
                    from lib.conversations.project_peer import _titles_by_conv

                    titles = _titles_by_conv(
                        need, user_id=owner_user_id)
                except Exception as e:
                    logger.debug("[Peer] feed title backfill failed: %s", e)
            meta["feedActivity"] = {
                "count": len(events),
                "events": [
                    {
                        "kind": ev.get("kind", "note"),
                        "title": (ev.get("title") or "").strip()
                        or titles.get(ev.get("conv_id"), ""),
                        "convId": ev.get("conv_id", ""),
                        # Forward the FULL summary — the feed row caps its DISPLAY
                        # summary at _SUMMARY_MAX_CHARS but preserves the untruncated
                        # text in payload['summary_full']; showing the capped value
                        # cut sentences off mid-word.
                        "summary": (ev.get("payload") or {}).get("summary_full")
                        or ev.get("summary", ""),
                        "ts": ev.get("ts", 0),
                        "mine": bool(
                            ev.get("conv_id") and ev.get("conv_id") == current_conv_id
                        ),
                    }
                    for ev in events
                ],
            }
            return
        # ── project_message / project_intervene → delivery descriptor ──
        if fn_name in ("project_message", "project_intervene"):
            to = (_fn_args.get("to_conv_id") or "").strip()
            text = (_fn_args.get("text") or _fn_args.get("message") or "").strip()
            content = (
                _tool_content if isinstance(_tool_content, str) else str(_tool_content)
            )
            low = content.lower()
            # Classify the outcome off the well-known result-string phrasing.
            if (
                low.startswith("error")
                or "was denied" in low
                or "requires explicit human" in low
            ):
                outcome = "failed"
            elif "not sent" in low or "rate limit" in low:
                outcome = "rate_limited"
            elif "denied" in low:
                outcome = "denied"
            else:
                outcome = "delivered"
            hard = (
                bool(_fn_args.get("hard_abort"))
                if fn_name == "project_intervene"
                else False
            )
            meta["peerDelivery"] = {
                "tool": fn_name,
                "toConv": to,
                "text": text,
                "hardAbort": hard,
                "outcome": outcome,
            }
            return

    return simple_call(
        task,
        fn_name,
        fn_args,
        rn,
        round_entry,
        tc_id,
        executor=_run,
        source="Peer",
        module_tag="Peer",
        badge=_verb,
        post_build=_post_build,
    )
