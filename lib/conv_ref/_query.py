"""Conversation reference — owner-scoped search and list rendering.

Select conversation metadata through ``lib.conversations.repository`` and
render the model-facing listing. Storage protocol details, database backends,
and transcript archives do not cross this module boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.identity import require_user_id


class ConversationListing(str):
    """Model text carrying a same-query browser receipt out of band."""

    def __new__(cls, text, receipt=None):
        result = super().__new__(cls, text)
        result.conversation_search = receipt
        return result


def _timestamp_label(value) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromtimestamp(
            int(value) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def list_conversations(
    keyword=None,
    limit=20,
    scope="auto",
    project_path=None,
    current_conv_id=None,
    *,
    user_id,
):
    """List other conversations visible to one owning principal.

    ``scope='project'`` filters on ``settings.projectPath``. Keyword matching
    combines a title substring with the authority's bounded full-text search;
    transcript archives are never loaded for a list operation.
    """
    owner_id = require_user_id(user_id, context="list conversation references")
    limit = min(max(1, int(limit or 20)), 50)
    effective_scope = scope or "auto"
    if effective_scope == "auto":
        effective_scope = "project" if project_path else "all"
    if effective_scope not in {"project", "all"}:
        raise ValueError("scope must be 'auto', 'project', or 'all'")
    if effective_scope == "project" and not project_path:
        raise ValueError("scope='project' requires a project context; use 'all' explicitly")

    from lib.conversations.repository import (
        list_conversations as read_conversations,
        search_conversation_page,
    )

    keyword_text = str(keyword or "").strip()
    body_hit_ids = set()
    coverage = 'ready'
    coverage_reasons = []
    cursor = None
    incomplete = False
    if keyword_text:
        # The v2 reference-tool contract bounds foreground work to four pages.
        # Empty pages with a cursor are not terminal zero-hit results.
        for _ in range(4):
            page = search_conversation_page(keyword_text, user_id=owner_id,
                                            limit=200, cursor=cursor)
            body_hit_ids.update(hit['id'] for hit in page['items'])
            coverage = page.get('coverage', 'unknown')
            coverage_reasons = list(dict.fromkeys([
                *coverage_reasons, *page.get('coverage_reasons', []),
            ]))[:8]
            cursor = page['next_cursor']
            incomplete = bool(cursor or page['incomplete'])
            if cursor is None:
                break
    # A non-search listing is already ordered and project-filtered by the
    # authority. Title matches and body-hit IDs are independently bounded,
    # then merged by the same authority order.
    candidate_limit = min(10_000, limit + (1 if current_conv_id else 0))
    common_read = {
        "user_id": owner_id,
        "project_path": (
            project_path if effective_scope == "project" else None
        ),
        "order_by": "updated_at_desc",
        "include_messages": False,
        "settings_keys": ["projectPath"],
    }
    if keyword_text:
        title_snapshots = read_conversations(
            **common_read,
            title_contains=keyword_text,
            limit=candidate_limit,
        )
        body_snapshots = (
            read_conversations(
                **common_read,
                ids=sorted(body_hit_ids),
                limit=len(body_hit_ids),
            )
            if body_hit_ids
            else []
        )
        snapshots_by_id = {
            str(snapshot.get("id") or ""): snapshot
            for snapshot in [*title_snapshots, *body_snapshots]
            if snapshot.get("id")
        }
        snapshots = sorted(
            snapshots_by_id.values(),
            key=lambda snapshot: (
                int(snapshot.get("updated_at") or 0),
                str(snapshot.get("id") or ""),
            ),
            reverse=True,
        )
    else:
        snapshots = read_conversations(
            **common_read,
            limit=candidate_limit,
        )

    keyword_lower = keyword_text.lower()
    selected = []
    for snapshot in snapshots:
        settings = snapshot.get("settings") or {}
        if (
            effective_scope == "project"
            and settings.get("projectPath") != project_path
        ):
            continue
        conversation_id = str(snapshot.get("id") or "")
        if not conversation_id or conversation_id == current_conv_id:
            continue
        if keyword_lower:
            title_hit = keyword_lower in str(snapshot.get("title") or "").lower()
            if not title_hit and conversation_id not in body_hit_ids:
                continue
        selected.append(snapshot)
        if len(selected) >= limit:
            break

    receipt = {
        'keyword': keyword_text, 'scope': effective_scope,
        'projectPath': project_path if effective_scope == 'project' else None,
        'returnedCount': len(selected), 'limit': limit,
        'coverage': coverage, 'incomplete': incomplete,
        'coverageReasons': coverage_reasons,
        'items': [{'id': str(row['id']), 'title': row.get('title') or ''}
                  for row in selected],
    }
    scope_note = (
        f" in this project ({project_path})"
        if effective_scope == "project" and project_path
        else " across your other conversations"
    )
    if not selected:
        if keyword_text:
            advice = "Try a shorter or different keyword."
            if effective_scope == 'project':
                advice += " Or pass scope='all' to search your other projects."
            status = ('No conversations returned' if incomplete or coverage != 'ready'
                      else 'No conversations found')
            warning = (' Search is incomplete or coverage is not ready; this is not proof of absence.'
                       if incomplete or coverage != 'ready' else '')
            return ConversationListing(
                f"{status} matching '{keyword_text}'{scope_note}. {advice}{warning} "
                "Search covers titles and indexed public text, not tool arguments/results.", receipt)
        return ConversationListing(f"No other conversations found{scope_note}.", receipt)

    keyword_note = f" matching '{keyword_text}'" if keyword_text else ''
    lines = [f"Found {len(selected)} conversation(s){keyword_note}{scope_note}:\n"]
    for snapshot in selected:
        conversation_id = snapshot["id"]
        title = snapshot.get("title") or "(untitled)"
        msg_count = snapshot.get("msg_count") or 0
        updated = snapshot.get("updated_at") or snapshot.get("created_at") or 0
        lines.append(
            f'• [{conversation_id}] "{title}" — {msg_count} messages, '
            f"updated {_timestamp_label(updated)}"
        )
    lines.append(
        '\nUse get_conversation(conversation_id="<id>") to retrieve full content.'
    )
    if incomplete or coverage != 'ready':
        lines.append('Search is incomplete or coverage is not ready; results are not exhaustive.')
    return ConversationListing("\n".join(lines), receipt)


__all__ = ["list_conversations"]
