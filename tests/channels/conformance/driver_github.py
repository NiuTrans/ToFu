"""GitHub conformance driver: today's ``GitHubAdapter`` behind the shim, against ``FakeGitHub``.

The adapter polls (C8): a "live" message is a comment that the next pass
reads, a redelivery is the documented second-granular ``since`` overlap, and
the driver runs passes exactly as the adapter's worker does (``_run``): a
rate limit pauses polling and sending until GitHub's resume time, an auth or
transport failure ends the session for the supervisor's next tick. The fake's
``responders`` inject GitHub's faults (5xx, 401, 403 rate limits, lost answers).
"""
from __future__ import annotations

import itertools
import re

from lib.channels.model import ThreadKey
from tests.channels.conftest import (
    GH_ALICE, GH_NOW, GH_TOKEN, GH_USER, OWNER, FakeGitHub, gh_http_date, gh_time,
)
from tests.channels.conformance.harness import Driver, Msg, NotApplicable, Shim, advance_core_clock

REPO = 'octo/repo'
THREAD = ThreadKey.github(REPO, 1)
GH_BOT = {'id': 49699333, 'login': 'dependabot[bot]', 'type': 'Bot'}
_MARKER = re.compile(r'\s*<!-- tofuOutbox:[^>]*-->\s*$')


class GitHubDriver(Driver):
    name = channel = 'github'

    def setup(self) -> None:
        self.fake = FakeGitHub()
        self.fake.issue(REPO, 1, title='Conformance', created=GH_NOW - 3600)
        entry = f'channel.u{OWNER}.github.token'
        self.env.vault.set_entry(entry, GH_TOKEN)
        self.store.update_account('github', lambda account: {
            **account, 'enabled': True,
            'identity': {'uid': str(GH_USER['id']), 'mis': GH_USER['login'], 'name': GH_USER['name']},
            'credential': {'name': entry, 'managed': True}, 'frontierAtMs': (GH_NOW - 600) * 1000})
        # watch: the thread is read on every pass; auto: the owner's own requests send without a draft.
        self.store.update_thread(THREAD, lambda value: {**value, 'mode': 'watch', 'replyPolicy': 'auto',
                                                        'title': 'Conformance'})
        from lib.channels.github import supervisor
        self.env.monkeypatch.setattr(supervisor, '_ADAPTERS', {})  # this test's supervisor registry
        self._ticks = itertools.count(1)
        self.starts: list = []
        self.hidden: dict[int, dict] = {}
        self.shim = Shim(self.env, 'github', sender_source=lambda: self.adapter)
        self.host = self.shim.host
        self._start()

    def _start(self) -> None:
        from lib.channels.github.adapter import GitHubAdapter, api_base_url, token_source
        from lib.channels.github.client import GitHubClient
        fake = self.fake

        def client(owner, account):
            return GitHubClient(api_base_url(account), token_source(account), transport=fake,
                                clock=lambda: fake.now * 1000)
        # A monotonic clock that has always passed GitHub's spacing between mutations.
        self.adapter = GitHubAdapter(OWNER, store=self.store, client_factory=client, clock=lambda: fake.now * 1000,
                                     monotonic=lambda: next(self._ticks) * 10.0)
        self.adapter.prepare()
        self.dead = False

    # the worker's own pass discipline (GitHubAdapter._run) -----------------
    def poll(self) -> None:
        from lib.channels.github.client import GitHubAuthError, GitHubHTTPError, GitHubRateLimited, GitHubUnavailable
        if self.dead or self.adapter._blocked_until > self.fake.now * 1000:
            return
        try:
            self.adapter.poll_once()
        except GitHubRateLimited as error:
            self.adapter._block(error.resume_at_ms)
        except GitHubAuthError as error:
            self.adapter._end('needs_login', str(error))
            self.dead = True
        except (GitHubUnavailable, GitHubHTTPError) as error:
            self.adapter._end('disconnected', str(error))
            self.dead = True

    def execute(self) -> None:
        from lib.channels.github.client import GitHubRateLimited
        if self.dead or self.adapter._blocked_until > self.fake.now * 1000:
            return
        try:
            self.shim.execute()
        except GitHubRateLimited as error:
            self.adapter._block(error.resume_at_ms)
        except Exception as error:
            self.last_execute_error = error

    def tick(self) -> None:
        """The supervisor restarts an ended session at its next tick; the pass follows."""
        if self.dead:
            self._start()
        self.poll()

    def connect(self) -> None:
        self.poll()
        assert not self.dead

    def alive(self) -> bool:
        return not self.dead

    def watched_thread(self) -> ThreadKey:
        return THREAD

    def send_thread(self) -> ThreadKey:
        return THREAD

    # inbound -----------------------------------------------------------
    def _comment(self, text: str, *, user=GH_ALICE, created: int | None = None) -> dict:
        self.fake.now += 1
        row = self.fake.comment(REPO, 1, user=user, body=text, created=created or self.fake.now)
        self.fake.notify(REPO, 1, title='Conformance', updated=self.fake.now)
        return row

    def post(self, thread: ThreadKey, text: str, *, user=GH_ALICE) -> Msg:
        row = self._comment(text, user=user)
        self.poll()
        return Msg(thread, text, str(row['id']), {'row': row})

    def redeliver(self, msg: Msg) -> None:
        # GitHub's since is second-granular and inclusive: a neighbour in the same second brings the row back.
        self.fake.comment(REPO, 1, user=GH_ALICE, body='neighbour of ' + msg.platform_id,
                          created=self.fake.now)
        self.poll()

    def pair(self, thread: ThreadKey) -> tuple[Msg, Msg]:
        older = self._comment('older ' + str(self.fake.now), created=self.fake.now - 30)
        rows = self.fake.comments[(REPO, 1)]
        rows.remove(older)
        self.hidden[older['id']] = older  # GitHub shows it only later (C8 Limits: visibility has no bound)
        newer = self._comment('newer ' + str(self.fake.now))
        return (Msg(thread, older['body'], str(older['id']), {'row': older}),
                Msg(thread, newer['body'], str(newer['id']), {'row': newer}))

    def deliver(self, msg: Msg) -> None:
        row = self.hidden.pop(msg.extra['row']['id'], None)
        if row is not None:
            self.fake.comments[(REPO, 1)].append(row)
        self.poll()

    def edit(self, msg: Msg, text: str) -> None:
        self.fake.now += 1
        msg.extra['row'].update(body=text, updated_at=gh_time(self.fake.now))
        self.poll()

    def recall(self, msg: Msg) -> None:
        raise NotApplicable('GitHub REST lists show no deletions, and C8 reads no events API')

    def bot_post(self, text: str) -> Msg:
        return self.post(THREAD, text, user=GH_BOT)

    # history -----------------------------------------------------------
    def post_unseen(self, thread: ThreadKey, text: str) -> Msg:
        row = self._comment(text)
        return Msg(thread, text, str(row['id']), {'row': row})

    def fail_next_history(self) -> None:
        fake, path = self.fake, f'/repos/{REPO}/issues/1/comments'

        def fail(record):
            if record['method'] == 'GET' and record['path'] == path:
                fake.responders.remove(fail)
                return fake.reply(502, {'message': 'Server Error'}, record['url'])
            return None
        fake.responders.append(fail)

    def reconnect(self) -> None:
        self.poll()  # a pass is the catch-up

    # outbound ----------------------------------------------------------
    def received(self, thread: ThreadKey) -> list[str]:
        return [_MARKER.sub('', row['body']) for row in self.fake.posted]

    def lose_next_answer(self) -> None:
        fake, path = self.fake, f'/repos/{REPO}/issues/1/comments'

        def lose(record):
            if record['method'] == 'POST' and record['path'] == path:
                fake.responders.remove(lose)
                fake._create((REPO, 1), record, dict(GH_USER), record['url'])
                return fake.reply(502, {'message': 'Bad Gateway'}, record['url'])
            return None
        fake.responders.append(lose)

    def send_threads(self) -> tuple[ThreadKey, ThreadKey]:
        second = ThreadKey.github(REPO, 2)
        self.fake.issue(REPO, 2, title='Second', created=GH_NOW - 3600)
        self.store.update_thread(second, lambda value: {**value, 'mode': 'watch', 'replyPolicy': 'auto',
                                                        'title': 'Second'})
        return THREAD, second

    def fail_reconciliation_reads(self, thread: ThreadKey) -> None:
        fake, path = self.fake, f'/repos/{thread.repository}/issues/{thread.number}/comments'

        def fail(record):
            if record['method'] == 'GET' and record['path'] == path:
                return fake.reply(502, {'message': 'Server Error'}, record['url'])
            return None
        self._read_failures = {**getattr(self, '_read_failures', {}), str(thread): fail}
        fake.responders.append(fail)

    def heal_reconciliation_reads(self, thread: ThreadKey) -> None:
        self.fake.responders.remove(self._read_failures.pop(str(thread)))

    def deliver_without_receipt(self, item) -> None:
        self.claim(item)
        self.adapter.send(THREAD, item.text, marker=item.id, reply_to_external_id=None)

    def restart(self) -> None:
        self._start()

    def rate_limit_sends(self) -> None:
        fake, path = self.fake, f'/repos/{REPO}/issues/1/comments'

        def limit(record):
            if record['method'] == 'POST' and record['path'] == path:
                return fake.reply(403, {'message': 'API rate limit exceeded'}, record['url'],
                                  {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': str(fake.now + 60)})
            return None
        self._limit = limit
        fake.responders.append(limit)

    def end_rate_limit(self) -> None:
        self.fake.responders.remove(self._limit)
        self.fake.now += 61
        self.fake.last_modified = gh_http_date(self.fake.now)
        advance_core_clock(self.env, 61_000)

    def attempts(self, item) -> int:
        return sum(1 for record in self.fake.requests if record['method'] == 'POST'
                   and item.id in ((record.get('body') or {}).get('body') or ''))

    def echo(self, item) -> None:
        self.poll()  # the owner's comment is in the watched thread the next pass reads

    def self_send_outside_outbox(self, text: str) -> Msg:
        """A comment of the owner's account posted by another Tofu path, read by the next pass."""
        return self.post(THREAD, text + '\n\n<!-- tofuOutbox:tool-1 -->', user=GH_USER)

    # faults ------------------------------------------------------------
    def expire_auth(self) -> None:
        self.fake.users.pop(GH_TOKEN)
        self.poll()

    def network_fault(self) -> None:
        def unreachable(record):
            raise ConnectionError('connection refused')
        self.fake.responders.append(unreachable)
        self.poll()

    def assert_no_restart(self, *, boot: bool = False) -> None:
        from lib.channels.github import supervisor
        if boot:
            self.env.monkeypatch.setattr(supervisor, '_ADAPTERS', {})
        before = len(self.starts)

        def factory(owner, **kwargs):
            self.starts.append(kwargs)
            raise AssertionError('the supervisor started a session')
        outcome = supervisor.reconcile_owner(OWNER, store=self.store, adapter_factory=factory)
        assert len(self.starts) == before, outcome
