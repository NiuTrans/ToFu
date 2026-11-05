"""GitHub payloads → ``ChannelEvent`` facts (C8). Pure functions: no policy, no I/O.

Responsibility: notification → thread, issue (opening post), issue comment
and pull request review comment → event, the event external ID, @-mention
and outbox-marker extraction.

External ID: ``<created_at epoch seconds, 10 digits><content kind, 1 digit>
<GitHub object id, 20 digits>``. Issues, issue comments and review comments
are separate GitHub ID sequences, so the kind digit keeps one thread's IDs
unique; the time prefix orders them like a snowflake (watermark, newest-first
reads). Kinds come from the contract (``x-github.contentKinds``).

Entry points: ``notification_thread``, ``issue_event``, ``comment_event``,
``external_id``, ``mentions``, ``outbox_marker``, ``marker_suffix``.
"""
from __future__ import annotations

import re
from typing import Any

from lib.channels.model import GITHUB, ChannelError, ChannelEvent, ThreadKey
from lib.channels.github.client import parse_time_ms

KIND_OPENING_POST = int(GITHUB['contentKinds']['openingPost'])
KIND_ISSUE_COMMENT = int(GITHUB['contentKinds']['issueComment'])
KIND_REVIEW_COMMENT = int(GITHUB['contentKinds']['reviewComment'])
_MARKER_TEMPLATE = GITHUB['outboxMarker']
_MARKER_RE = re.compile(re.escape(_MARKER_TEMPLATE).replace(re.escape('{item}'), '(ob[0-9a-f]{32})'))
# Notification subjects of an issue or pull request; other subjects (releases,
# discussions, commits, check suites, alerts) are not comment threads.
_SUBJECT_TYPES = {'Issue': False, 'PullRequest': True}
# The subject URL is the only place a notification names its number.
_SUBJECT_URL_RE = re.compile(r'/(issues|pulls)/([1-9][0-9]{0,9})$')
# Code is not prose: GitHub does not notify mentions inside code.
_FENCE_RE = re.compile(r'^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)', re.S | re.M)
_INLINE_CODE_RE = re.compile(r'(`+)(?:(?!\1).)+?\1', re.S)
# GitHub login syntax after '@', not part of an e-mail address, not a team (@org/team).
_MENTION_RE = re.compile(r'(?<![A-Za-z0-9_`@/.-])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![A-Za-z0-9_-]|/[A-Za-z0-9])')


def external_id(created_ms: int, kind: int, object_id: int) -> str:
    if not 0 < int(object_id) < 10 ** 20 or not 0 <= int(created_ms) // 1000 < 10 ** 10:
        raise ChannelError('invalid_external_id', 'GitHub object outside the event ID range')
    return f'{int(created_ms) // 1000:010d}{int(kind)}{int(object_id):020d}'


def marker_suffix(item_id: str) -> str:
    """The invisible marker appended to a posted comment (echo + reconciliation)."""
    return '\n\n' + _MARKER_TEMPLATE.replace('{item}', item_id)


def outbox_marker(body: str) -> str:
    found = _MARKER_RE.findall(body or '')
    return found[-1] if found else ''


def mentions(body: str, login: str) -> bool:
    """Whether ``body`` @-mentions ``login`` outside code (GitHub logins are case-insensitive)."""
    if not body or not login:
        return False
    prose = _INLINE_CODE_RE.sub(' ', _FENCE_RE.sub('\n', body))
    target = login.lower()
    return any(name.lower() == target for name in _MENTION_RE.findall(prose))


def notification_thread(item: Any) -> tuple[ThreadKey, str, bool] | None:
    """``(thread, title, is_pull_request)`` of an issue/PR notification, else None."""
    if not isinstance(item, dict):
        return None
    subject = item.get('subject') if isinstance(item.get('subject'), dict) else {}
    repository = item.get('repository') if isinstance(item.get('repository'), dict) else {}
    is_pull = _SUBJECT_TYPES.get(str(subject.get('type') or ''))
    match = _SUBJECT_URL_RE.search(str(subject.get('url') or ''))
    if is_pull is None or match is None or not repository.get('full_name'):
        return None
    try:
        thread = ThreadKey.github(str(repository['full_name']), int(match.group(2)))
    except ChannelError:
        return None
    return thread, str(subject.get('title') or ''), is_pull


def _user(value: Any) -> tuple[str, str]:
    user = value if isinstance(value, dict) else {}
    uid = user.get('id')
    return (str(uid) if isinstance(uid, int) and not isinstance(uid, bool) else ''), str(user.get('login') or '')


def _mentioned(body: str, identity: dict[str, Any]) -> tuple[str, ...]:
    owner = str(identity.get('uid') or '')
    return (owner,) if owner and mentions(body, str(identity.get('mis') or '')) else ()


def issue_event(issue: Any, thread: ThreadKey, identity: dict[str, Any]) -> ChannelEvent | None:
    """The opening post of an issue or pull request (title and body)."""
    if not isinstance(issue, dict) or not isinstance(issue.get('id'), int):
        return None
    created = parse_time_ms(issue.get('created_at'))
    if created is None:
        return None
    title = str(issue.get('title') or '')
    body = issue.get('body') if isinstance(issue.get('body'), str) else ''
    sender_id, sender_name = _user(issue.get('user'))
    return ChannelEvent(
        thread=thread, external_id=external_id(created, KIND_OPENING_POST, issue['id']),
        sender_id=sender_id, sender_name=sender_name, text=f'{title}\n\n{body}'.strip(),
        sent_at_ms=created, thread_title=title, message_type=KIND_OPENING_POST,
        mentioned_ids=_mentioned(body, identity), outbox_marker=outbox_marker(body),
        url=str(issue.get('html_url') or ''))


def comment_event(comment: Any, thread: ThreadKey, *, kind: int, identity: dict[str, Any],
                  title: str = '', reply_to: tuple[str, str] = ('', '')) -> ChannelEvent | None:
    """An issue comment or review comment; ``reply_to`` = (parent external ID, parent author ID)."""
    if not isinstance(comment, dict) or not isinstance(comment.get('id'), int):
        return None
    created = parse_time_ms(comment.get('created_at'))
    if created is None:
        return None
    body = comment.get('body') if isinstance(comment.get('body'), str) else ''
    sender_id, sender_name = _user(comment.get('user'))
    return ChannelEvent(
        thread=thread, external_id=external_id(created, kind, comment['id']),
        sender_id=sender_id, sender_name=sender_name, text=body, sent_at_ms=created,
        thread_title=title, message_type=kind, mentioned_ids=_mentioned(body, identity),
        reply_to_external_id=reply_to[0], reply_to_sender_id=reply_to[1],
        outbox_marker=outbox_marker(body), url=str(comment.get('html_url') or ''))


def comment_reference(comment: Any, kind: int) -> tuple[str, str]:
    """``(external ID, author ID)`` of a comment, used as another comment's reply target."""
    if not isinstance(comment, dict) or not isinstance(comment.get('id'), int):
        return '', ''
    created = parse_time_ms(comment.get('created_at'))
    if created is None:
        return '', ''
    return external_id(created, kind, comment['id']), _user(comment.get('user'))[0]


__all__ = ['KIND_ISSUE_COMMENT', 'KIND_OPENING_POST', 'KIND_REVIEW_COMMENT', 'comment_event',
           'comment_reference', 'external_id', 'issue_event', 'marker_suffix', 'mentions',
           'notification_thread', 'outbox_marker']
