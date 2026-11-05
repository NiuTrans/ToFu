"""GitHub channel adapter: one owner's poll worker and outbox sender (C8).

Responsibility: one serial worker per owner (GitHub asks integrations not to
make concurrent requests) that

1. polls ``GET /notifications?all=true&since=<frontier>`` with the stored
   ``Last-Modified`` as ``If-Modified-Since`` (``304`` = nothing changed), at
   the cadence of GitHub's ``X-Poll-Interval`` header — the only cadence;
   before GitHub has sent one, the scheduler's existing tick triggers passes;
2. reads every notified issue or pull request thread and every thread in
   ``watch`` mode: the opening post once, issue comments and, for pull
   requests, review comments (``?since=`` per list, kept stable with its
   ETag so an unchanged thread answers ``304``), resolving review-reply
   targets;
3. ingests the events through ``lib.channels.ingress`` (record once, then
   route) and only afterwards advances the per-thread cursors and ETags, the
   account frontier and ``Last-Modified``: a failed route replays the window
   and dedup turns it into duplicates, never losses;
4. executes the GitHub outbox: ``POST .../issues/{n}/comments`` with the
   item's invisible marker, mutative requests spaced as GitHub's REST
   guidance asks, and reconciliation of an unknown result over the owner's
   comments in the thread before any resend.

A rate limit pauses the worker until GitHub's resume time; a refused token
(401) ends the session as ``needs_login``; other failures end it so the
supervisor restarts it at its next tick.

Entry points: ``GitHubAdapter`` (``start``/``stop``/``tick``/``alive``,
``poll_once``, sender port ``send``/``history_since``), ``client_for``,
``account_binding``, ``due_work``. Dependencies: ``client``, ``normalize``,
``lib.channels`` (ingress, outbox, store, policy), ``lib.credentials_vault``.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from typing import Any

from lib.channels.github.client import (
    DEFAULT_API_BASE_URL, GitHubAuthError, GitHubClient, GitHubHTTPError, GitHubRateLimited,
    GitHubUnavailable, iso_seconds, parse_time_ms,
)
from lib.channels.github.normalize import (
    KIND_ISSUE_COMMENT, KIND_REVIEW_COMMENT, comment_event, comment_reference, issue_event,
    marker_suffix, notification_thread,
)
from lib.channels.model import GITHUB, ChannelEvent, ThreadKey
from lib.channels.store import ChannelStore, now_ms
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'github'
NOTIFICATIONS_PER_PAGE = int(GITHUB['notificationsPerPage'])
LIST_PER_PAGE = int(GITHUB['listPerPage'])
MUTATION_SPACING_S = int(GITHUB['mutationSpacingMs']) / 1000
_SEND_FAILURES = {400: 'invalid_input', 422: 'invalid_input', 403: 'forbidden',
                  404: 'unsupported_target', 410: 'unsupported_target'}


def api_base_url(account: dict[str, Any] | None) -> str:
    return str((account or {}).get('apiBaseUrl') or DEFAULT_API_BASE_URL)


def token_source(account: dict[str, Any] | None) -> Callable[[], str]:
    """Read the token from the credential vault at request time (never cached here)."""
    name = str(((account or {}).get('credential') or {}).get('name') or '')

    def read() -> str:
        from lib.credentials_vault import get_entry
        value = get_entry(name) if name else None
        if not value:
            raise GitHubAuthError('The GitHub token is missing from the credential vault')
        return value
    return read


def client_for(owner: int, account: dict[str, Any]) -> GitHubClient:
    return GitHubClient(api_base_url(account), token_source(account))


def account_binding(account: dict[str, Any] | None) -> tuple[str, str, str]:
    """What a running session is bound to: identity, credential entry and API base."""
    account = account or {}
    return (str((account.get('identity') or {}).get('uid') or ''),
            str((account.get('credential') or {}).get('name') or ''), api_base_url(account))


def due_work(now: int, *, next_poll_at: int | None, poll_requested: bool, outbox_pending: bool,
             blocked_until: int) -> tuple[bool, bool, float | None]:
    """``(run_outbox, run_poll, wait_seconds)`` for the worker.

    A known ``X-Poll-Interval`` (``next_poll_at``) alone decides when to poll;
    without one, a scheduler tick (``poll_requested``) does. While GitHub's rate
    limit is in force nothing runs. ``wait_seconds`` None waits for a wake.
    """
    if blocked_until > now:
        return False, False, (blocked_until - now) / 1000
    poll = (now >= next_poll_at) if next_poll_at is not None else poll_requested
    if poll or outbox_pending:
        return outbox_pending, poll, 0.0
    return False, False, None if next_poll_at is None else (next_poll_at - now) / 1000


class GitHubAdapter:
    """One owner's GitHub session: a serial poll/outbox worker."""

    def __init__(self, owner: int, *, store: ChannelStore | None = None,
                 client_factory: Callable[[int, dict[str, Any]], GitHubClient] | None = None,
                 clock: Callable[[], int] = now_ms, monotonic: Callable[[], float] = time.monotonic):
        self.owner = int(owner)
        self.store = store or ChannelStore(owner)
        self._client_factory = client_factory or client_for
        self._client: GitHubClient | None = None
        self._clock = clock
        self._monotonic = monotonic
        self._identity: dict[str, Any] = {}
        self._stopping = threading.Event()
        self._wake = threading.Condition()
        self._outbox_pending = True
        self._poll_requested = True
        self._next_poll_at: int | None = None
        self._blocked_until = 0
        self._last_mutation: float | None = None
        self._connected = False
        self._worker: threading.Thread | None = None
        self.binding: tuple[str, str, str] = ('', '', '')

    # lifecycle -------------------------------------------------------
    def prepare(self) -> None:
        """Bind the account's identity, client and persisted poll schedule."""
        account = self.store.account(CHANNEL) or {}
        self._identity = dict(account.get('identity') or {})
        self.binding = account_binding(account)
        self._client = self._client_factory(self.owner, account)
        next_at = (account.get('poll') or {}).get('nextAtMs')
        self._next_poll_at = int(next_at) if next_at else None

    def start(self) -> None:
        self.prepare()
        from lib.channels import outbox
        outbox.register_executor(self.owner, self.wake_executor, channel=CHANNEL)
        self._worker = threading.Thread(target=self._run, name=f'github-channel-{self.owner}',
                                        daemon=True)
        self._worker.start()

    def alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def stop(self, timeout: float) -> bool:
        self._stopping.set()
        from lib.channels import outbox
        outbox.register_executor(self.owner, None, channel=CHANNEL)
        with self._wake:
            self._wake.notify_all()
        if self._worker is not None:
            self._worker.join(timeout)
        return not self.alive()

    def tick(self) -> None:
        """Supervisor tick: a pass runs only when due (see ``due_work``)."""
        with self._wake:
            self._poll_requested = True
            self._outbox_pending = True
            self._wake.notify_all()

    def wake_executor(self) -> None:
        with self._wake:
            self._outbox_pending = True
            self._wake.notify_all()

    # worker ----------------------------------------------------------
    def _await_work(self) -> tuple[bool, bool]:
        with self._wake:
            while not self._stopping.is_set():
                run_outbox, run_poll, wait = due_work(
                    self._clock(), next_poll_at=self._next_poll_at,
                    poll_requested=self._poll_requested, outbox_pending=self._outbox_pending,
                    blocked_until=self._blocked_until)
                if run_outbox or run_poll:
                    self._outbox_pending = self._outbox_pending and not run_outbox
                    self._poll_requested = self._poll_requested and not run_poll
                    return run_outbox, run_poll
                self._wake.wait(wait)
        return False, False

    def _run(self) -> None:
        try:
            while not self._stopping.is_set():
                run_outbox, run_poll = self._await_work()
                if run_outbox:
                    self._outbox_pass()
                if run_poll and not self._stopping.is_set():
                    try:
                        self.poll_once()
                    except GitHubRateLimited as error:
                        self._block(error.resume_at_ms)
        except GitHubAuthError as error:
            self._end('needs_login', str(error))
        except (GitHubUnavailable, GitHubHTTPError) as error:
            self._end('disconnected', str(error))
        except Exception as error:
            code = getattr(error, 'code', type(error).__name__)
            if not self._stopping.is_set():
                logger.warning('[Channels] github session failed owner=%s: %s', self.owner, code,
                               exc_info=True)
            # Ending before the cursors move makes the next session replay the window.
            self._end('error', f'poll failed: {code}')

    def _end(self, state: str, detail: str) -> None:
        """Record why the session ended; the supervisor restarts it unless needs_login."""
        if not self._stopping.is_set():
            self.store.set_status(CHANNEL, state, detail)

    def _block(self, resume_at_ms: int) -> None:
        resume = max(int(resume_at_ms), self._clock())
        self._blocked_until = resume
        self._next_poll_at = max(self._next_poll_at or 0, resume)

        def change(account):
            poll = dict(account.get('poll') or {})
            poll['nextAtMs'] = max(int(poll.get('nextAtMs') or 0), resume)
            account['poll'] = poll
            return account
        self.store.update_account(CHANNEL, change)
        self._connected = False
        self.store.set_status(CHANNEL, 'disconnected',
                              f'GitHub rate limit; resuming at {iso_seconds(resume)}')

    def _outbox_pass(self) -> None:
        from lib.channels import outbox
        try:
            outbox.execute(self.owner, self, channel=CHANNEL, store=self.store)
        except GitHubRateLimited as error:
            self._block(error.resume_at_ms)
        except GitHubAuthError:
            raise
        except Exception:
            logger.warning('[Channels] github outbox pass failed owner=%s', self.owner, exc_info=True)

    # ingress ---------------------------------------------------------
    def _record_schedule(self, interval_s: int | None, started: int) -> None:
        """Persist GitHub's poll interval before any processing, so no restart polls early."""
        def change(account):
            poll = dict(account.get('poll') or {})
            if interval_s is not None:
                poll['intervalS'] = interval_s
            poll['lastAtMs'] = started
            known = poll.get('intervalS')
            poll['nextAtMs'] = started + int(known) * 1000 if known else None
            account['poll'] = poll
            return account
        poll = self.store.update_account(CHANNEL, change)['poll']
        self._next_poll_at = poll['nextAtMs']

    def poll_once(self) -> dict[str, Any]:
        """One pass: notifications, notified and watched threads, ingest, then cursors."""
        from lib.channels.ingress import ingest_batch
        from lib.channels.policy import effective_mode
        account = self.store.account(CHANNEL) or {}
        if not account.get('enabled'):
            return {'status': 'disabled', 'threads': [], 'events': 0}
        poll = account.get('poll') or {}
        frontier = int(account.get('frontierAtMs') or 0)
        started = self._clock()
        params: dict[str, Any] = {'all': 'true', 'per_page': NOTIFICATIONS_PER_PAGE}
        if frontier:
            params['since'] = iso_seconds(frontier)
        pages = self._client.pages('/notifications', params,
                                   if_modified_since=str(poll.get('lastModified') or ''))
        first = next(pages)
        self._record_schedule(first.poll_interval_s, started)
        # Mode-change times are this host's clock; GitHub's ``since`` is its own.
        skew = started - first.date_ms if first.date_ms else 0
        notified: dict[str, tuple[ThreadKey, str, bool]] = {}
        newest = 0
        if first.status == 200:
            for page in itertools.chain([first], pages):
                for item in page.json() or []:
                    parsed = notification_thread(item)
                    if parsed is None:
                        continue
                    notified[str(parsed[0])] = parsed
                    newest = max(newest, parse_time_ms(item.get('updated_at')) or 0)
        records = {record['key']: record for record in self.store.threads(CHANNEL)
                   if isinstance(record.get('key'), str)}
        watched = {key for key, record in records.items() if record.get('mode') == 'watch'
                   and not (record.get('github') or {}).get('unavailable')}
        events: list[ChannelEvent] = []
        updates: dict[str, tuple[dict[str, Any], str]] = {}
        for key in sorted(set(notified) | watched):
            thread, title, is_pull = notified.get(key) or (ThreadKey.parse(key), '', None)
            record = records.get(key) or {}
            if effective_mode(thread, record, account) == 'ignore':
                updates[key] = (dict(record.get('github') or {}), title)
                continue
            found, state, title = self._read_thread(thread, record, title, is_pull,
                                                    frontier or started - skew, skew)
            events.extend(found)
            updates[key] = (state, title)
        if events:
            events.sort(key=lambda event: (event.sent_at_ms, int(event.external_id)))
            ingest_batch(self.owner, events, store=self.store)
        for key, (state, title) in updates.items():
            self.store.update_thread(ThreadKey.parse(key), lambda value, state=state, title=title: {
                **value, 'github': state, 'title': title or value.get('title') or ''})
        last_modified = first.last_modified if first.status == 200 else ''

        def finish(account):
            if last_modified:
                account['poll'] = {**(account.get('poll') or {}), 'lastModified': last_modified}
            if newest > int(account.get('frontierAtMs') or 0):
                account['frontierAtMs'] = newest
            return account
        self.store.update_account(CHANNEL, finish)
        if not self._connected:
            self._connected = True
            self.store.set_status(CHANNEL, 'connected', '', connected_at_ms=self._clock())
        return {'status': first.status, 'threads': sorted(updates), 'events': len(events)}

    def _read_thread(self, thread: ThreadKey, record: dict[str, Any], title: str,
                     is_pull: bool | None, frontier: int,
                     skew: int) -> tuple[list[ChannelEvent], dict[str, Any], str]:
        """Events of one thread since its cursors, plus the thread's next GitHub state.

        ``skew`` (this host's clock minus GitHub's ``Date``) moves owner decision
        times, recorded on this host, onto GitHub's clock.
        """
        state = dict(record.get('github') or {})
        state.pop('unavailable', None)
        title = title or str(record.get('title') or '')
        events: list[ChannelEvent] = []
        # An ignored past (sinceMs) and the time before the last mode change
        # are never replayed; a list without a cursor starts at the frontier.
        decided = max(int(record.get('sinceMs') or 0), int(record.get('modeChangedAtMs') or 0))
        floor = max(decided - skew, 0) if decided else 0
        start = max(frontier, floor)
        base = f'/repos/{thread.repository}/issues/{thread.number}'
        if not state.get('createdAtMs'):
            response = self._client.get(base, allow_missing=True)
            if response.status in (404, 410):
                return [], {**state, 'unavailable': True}, title
            issue = response.json() or {}
            created = parse_time_ms(issue.get('created_at')) or 0
            state.update(createdAtMs=created, isPullRequest=isinstance(issue.get('pull_request'), dict),
                         htmlUrl=str(issue.get('html_url') or ''))
            title = str(issue.get('title') or title)
            if created > start:
                event = issue_event(issue, thread, self._identity)
                if event is not None:
                    events.append(event)
        if is_pull is None:
            is_pull = bool(state.get('isPullRequest'))
        lists = [('issueComments', base + '/comments', KIND_ISSUE_COMMENT)]
        if is_pull:
            lists.append(('reviewComments', f'/repos/{thread.repository}/pulls/{thread.number}/comments',
                          KIND_REVIEW_COMMENT))
        cursors = dict(state.get('cursors') or {})
        etags = dict(state.get('etags') or {})
        for name, path, kind in lists:
            cursor = int(cursors.get(name) or 0)
            since = iso_seconds(max(cursor, floor) if cursor else start)
            known = etags.get(name) or {}
            rows: list[dict[str, Any]] = []
            first = None
            for page in self._client.pages(path, {'since': since, 'per_page': LIST_PER_PAGE},
                                           etag=str(known.get('etag') or '') if known.get('since') == since else '',
                                           allow_missing=True):
                first = first or page
                if page.status != 200:
                    break
                rows.extend(row for row in page.json() or [] if isinstance(row, dict))
            if first.status in (404, 410):
                return [], {**state, 'unavailable': True}, title
            if first.status == 304:
                continue
            etags[name] = {'since': since, 'etag': first.etag}
            if rows:
                # A stable since keeps the ETag valid; it moves only past seen rows.
                cursors[name] = max(parse_time_ms(row.get('updated_at')) or 0 for row in rows)
            events.extend(self._comment_events(thread, rows, kind, title))
        state.update(cursors=cursors, etags=etags)
        return events, state, title

    def _comment_events(self, thread: ThreadKey, rows: list[dict[str, Any]], kind: int,
                        title: str) -> list[ChannelEvent]:
        references = {row['id']: comment_reference(row, kind) for row in rows
                      if isinstance(row.get('id'), int)}
        events = []
        for row in rows:
            parent = row.get('in_reply_to_id') if kind == KIND_REVIEW_COMMENT else None
            reply = ('', '')
            if isinstance(parent, int) and not isinstance(parent, bool):
                if parent not in references:
                    references[parent] = self._review_comment_reference(thread, parent)
                reply = references[parent]
            event = comment_event(row, thread, kind=kind, identity=self._identity, title=title,
                                  reply_to=reply)
            if event is not None:
                events.append(event)
        return events

    def _review_comment_reference(self, thread: ThreadKey, comment_id: int) -> tuple[str, str]:
        response = self._client.get(f'/repos/{thread.repository}/pulls/comments/{comment_id}',
                                    allow_missing=True)
        if response.status != 200:
            return '', ''
        return comment_reference(response.json(), KIND_REVIEW_COMMENT)

    # outbox sender port ----------------------------------------------
    def _space_mutation(self) -> None:
        if self._last_mutation is not None:
            remaining = self._last_mutation + MUTATION_SPACING_S - self._monotonic()
            if remaining > 0:
                self._stopping.wait(remaining)

    def send(self, thread: ThreadKey, text: str, *, marker: str,
             reply_to_external_id: str | None) -> dict[str, Any]:
        if thread.channel != CHANNEL:
            return {'ok': False, 'error': {'code': 'unsupported_target'}}
        if reply_to_external_id:
            return {'ok': False, 'error': {'code': 'invalid_input'}}
        self._space_mutation()
        try:
            response = self._client.request(
                'POST', f'/repos/{thread.repository}/issues/{thread.number}/comments',
                payload={'body': text + marker_suffix(marker)})
        except GitHubRateLimited as error:
            # Nothing was created; the unknown item is reconciled after the limit.
            self._block(error.resume_at_ms)
            return {'ok': False, 'error': {'code': 'rate_limited'}}
        except GitHubAuthError:
            return {'ok': False, 'error': {'code': 'auth_failed'}}
        except GitHubHTTPError as error:
            return {'ok': False, 'error': {'code': _SEND_FAILURES.get(error.status, 'unknown'),
                                           'message': str(error)}}
        except GitHubUnavailable as error:
            return {'ok': False, 'error': {'code': 'unknown', 'message': str(error)}}
        finally:
            self._last_mutation = self._monotonic()
        if response.status != 201:
            return {'ok': False, 'error': {'code': 'unknown'}}
        external, _ = comment_reference(response.json(), KIND_ISSUE_COMMENT)
        return {'ok': True, 'externalId': external, 'clientId': ''}

    def history_since(self, thread: ThreadKey, since_ms: int) -> list[ChannelEvent]:
        """The owner's comments in ``thread`` (a superset of the window after ``since_ms``).

        Reconciliation matches the exact marker, so the whole thread is read and
        GitHub's clock is never compared with this host's.
        """
        owner = str(self._identity.get('uid') or '')
        found = []
        for page in self._client.pages(f'/repos/{thread.repository}/issues/{thread.number}/comments',
                                       {'per_page': LIST_PER_PAGE}):
            for row in page.json() or []:
                event = comment_event(row, thread, kind=KIND_ISSUE_COMMENT, identity=self._identity)
                if event is not None and owner and event.sender_id == owner:
                    found.append(event)
        return found


__all__ = ['CHANNEL', 'GitHubAdapter', 'account_binding', 'api_base_url', 'client_for', 'due_work',
           'token_source']
