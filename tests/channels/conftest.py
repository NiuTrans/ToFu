"""Shared fakes for the channel suite: record store, router, turn dispatch, mailbox.

All fakes are per-test (function-scoped fixtures) and hermetic: no storage
sidecar, network, model or 大象 process.
"""
from __future__ import annotations

import copy

import pytest

from lib.storage.errors import StorageError

OWNER = 7
ROUTER = 'router-conv'
OWNER_UID = '1001'


class RecordClient:
    """In-memory ``record.*`` authority with the real CAS/list contract."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.fail_put = None  # optional callable(payload) -> raise

    def __call__(self, write=False):
        return self

    def query(self, operation, payload, deadline=None):
        namespace = payload['namespace']
        if operation == 'record.get':
            row = self.rows.get((namespace, payload['key']))
            return copy.deepcopy(row) if row else None
        assert operation == 'record.list'
        prefix = payload.get('prefix', '')
        after, before = payload.get('after_key', ''), payload.get('before_key', '')
        keys = sorted(key for (ns, key) in self.rows if ns == namespace and key.startswith(prefix)
                      and (not after or key > after) and (not before or key < before))
        if before:
            keys.reverse()
        rows = [{'key': key, **copy.deepcopy(self.rows[(namespace, key)])}
                for key in keys[:payload.get('limit', 100)]]
        if payload.get('include_value') is False:
            rows = [{k: v for k, v in row.items() if k != 'value'} for row in rows]
        return rows

    def command(self, operation, payload, command_id, priority='user', deadline=None):
        assert command_id
        key = (payload['namespace'], payload['key'])
        current = self.rows.get(key)
        actual = current['version'] if current else 0
        if 'expected_version' in payload and payload['expected_version'] != actual:
            raise StorageError('database_conflict', 'stale record')
        if operation == 'record.delete':
            self.rows.pop(key, None)
            return {'deleted': True}
        assert operation == 'record.put'
        if self.fail_put is not None:
            self.fail_put(payload)
        self.rows[key] = {'value': copy.deepcopy(payload['value']), 'version': actual + 1,
                          'updated_at_ms': 1}
        return {'key': payload['key'], 'version': actual + 1}

    def keys(self, part):
        return sorted(key for (_, key) in self.rows if part in key)


@pytest.fixture
def records(monkeypatch):
    client = RecordClient()
    monkeypatch.setattr('lib.storage.get_storage_client', client)
    return client


@pytest.fixture
def store(records):
    from lib.channels.store import ChannelStore
    return ChannelStore(OWNER, client=records)


@pytest.fixture
def router(monkeypatch):
    """A Global Router binding provided through the router package interface.

    Patches the real ``lib.router.binding`` attribute: replacing the package in
    ``sys.modules`` breaks modules importing other ``lib.router`` submodules
    (the HTTP routes import ``lib.router.contract``).
    """
    calls = []

    def global_router(user_id, *, create):
        calls.append((user_id, create))
        return {'conversationId': ROUTER, 'projectPath': '/srv/router-home',
                'revision': 1, 'availability': 'available'}

    monkeypatch.setattr('lib.router.binding.global_router', global_router)
    return calls


class Dispatcher:
    def __init__(self):
        self.calls = []
        self.outcomes: dict[str, str] = {}

    def __call__(self, conversation_id, user_message, tools_config, **kwargs):
        from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
        self.calls.append({'conversation_id': conversation_id, 'message': copy.deepcopy(user_message),
                           **kwargs})
        disposition = self.outcomes.get(conversation_id, 'started')
        return ScheduledTurnDispatch(disposition, task_id='t' if disposition == 'started' else '')

    def to(self, conversation_id):
        return [call for call in self.calls if call['conversation_id'] == conversation_id]


@pytest.fixture
def dispatch(monkeypatch):
    recorder = Dispatcher()
    monkeypatch.setattr('lib.scheduler.conversation_dispatch.dispatch_scheduled_turn', recorder)
    return recorder


@pytest.fixture
def turns(monkeypatch):
    """Durable Turn reads (``lib.turn_lifecycle.get_turn``): rows by (conversation, turn); none by default.

    Channel tools judge the authority of the calling Turn (C11), so tests that
    call them read Turns from here instead of the storage authority.
    """
    rows = {}

    def get_turn(conversation_id, turn_id, *, user_id, include_projection=True):
        return rows.get((conversation_id, turn_id))
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', get_turn)
    return rows


class Mailbox:
    submitted: list = []

    def __init__(self, owner, project, recipient, *, client=None):
        self.owner, self.project, self.recipient = owner, project, recipient

    def submit(self, sender, args, task_id):
        Mailbox.submitted.append({'owner': self.owner, 'project': self.project,
                                  'recipient': self.recipient, 'sender': sender, **args})
        return {'id': args['idempotency_key']}


@pytest.fixture
def mailbox(monkeypatch):
    Mailbox.submitted = []
    monkeypatch.setattr('lib.conversations.project_messages.ProjectMailbox', Mailbox)
    return Mailbox.submitted


@pytest.fixture
def enabled(store):
    """An enabled 大象 account bound to OWNER_UID."""
    def configure(command_chat=None):
        store.update_account('dx', lambda account: {
            **account, 'enabled': True,
            'identity': {'uid': OWNER_UID, 'mis': 'owner', 'name': 'Owner'},
            'commandChat': command_chat, 'frontierAtMs': 1})
        return store.account('dx')
    return configure


def make_event(thread='dx:gid:500', external_id='7000000000000000001', *, sender='2002',
               text='hello', title='', **extra):
    from lib.channels.model import ChannelEvent, ThreadKey
    return ChannelEvent(thread=ThreadKey.parse(thread), external_id=external_id, sender_id=sender,
                        sender_name=f'user{sender}', text=text,
                        sent_at_ms=1_700_000_000_000 + int(external_id) % 1000,
                        thread_title=title, **extra)


# ── GitHub (C8): a hermetic REST server behind the client transport seam ──

GH_TOKEN = 'ghp_FAKEchannelTokenOwner7DoNotUse0001'  # synthetic, never a real credential
GH_USER = {'id': 583231, 'login': 'Octo-Owner', 'name': 'Octo Owner', 'type': 'User'}
GH_ALICE = {'id': 1001, 'login': 'alice', 'type': 'User'}
GH_BOB = {'id': 1002, 'login': 'bob', 'type': 'User'}
GH_NOW = 1_793_000_000  # GitHub server time (epoch seconds) of the fake


def gh_time(seconds: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def gh_http_date(seconds: int) -> str:
    import email.utils
    return email.utils.formatdate(seconds, usegmt=True)


class FakeGitHub:
    """GitHub REST semantics the adapter relies on; records every request, no network.

    Implements GET /user, GET /notifications (since, all, per_page, Link pages,
    Last-Modified/If-Modified-Since → 304, X-Poll-Interval), issues, issue and
    review comment lists (since, per_page, Link pages, ETag/If-None-Match →
    304), one review comment, and POST issue comments. ``responders`` may
    answer a request first (fault injection).
    """

    def __init__(self):
        self.users = {GH_TOKEN: dict(GH_USER)}
        self.requests: list[dict] = []
        self.notifications: list[dict] = []
        self.last_modified = gh_http_date(GH_NOW - 3600)
        self.poll_interval = '60'
        self.now = GH_NOW
        self.issues: dict[tuple[str, int], dict] = {}
        self.comments: dict[tuple[str, int], list[dict]] = {}
        self.review_comments: dict[tuple[str, int], list[dict]] = {}
        self.posted: list[dict] = []
        self.responders: list = []
        self._ids = 700000

    # world ---------------------------------------------------------------
    def issue(self, repo, number, *, title, created, user=None, pull=False, body=''):
        kind = 'pull' if pull else 'issues'
        issue = {'id': 50_000 + number, 'number': number, 'title': title, 'body': body,
                 'user': user or GH_ALICE, 'created_at': gh_time(created), 'updated_at': gh_time(created),
                 'html_url': f'https://github.com/{repo}/{kind}/{number}'}
        if pull:
            issue['pull_request'] = {'url': f'https://api.github.com/repos/{repo}/pulls/{number}'}
        self.issues[(repo.lower(), number)] = issue
        return issue

    def comment(self, repo, number, *, user, body, created, review=False, reply_to=None, updated=None):
        self._ids += 1
        row = {'id': self._ids, 'body': body, 'user': user, 'created_at': gh_time(created),
               'updated_at': gh_time(updated or created),
               'html_url': f'https://github.com/{repo}/issues/{number}#issuecomment-{self._ids}'}
        if review:
            row['in_reply_to_id'] = reply_to
            row['html_url'] = f'https://github.com/{repo}/pull/{number}#discussion_r{self._ids}'
            row['pull_request_url'] = f'https://api.github.com/repos/{repo}/pulls/{number}'
            self.review_comments.setdefault((repo.lower(), number), []).append(row)
        else:
            row['issue_url'] = f'https://api.github.com/repos/{repo}/issues/{number}'
            self.comments.setdefault((repo.lower(), number), []).append(row)
        return row

    def notify(self, repo, number, *, title, updated, pull=False, reason='mention'):
        kind = 'pulls' if pull else 'issues'
        self.notifications = [item for item in self.notifications
                              if item['subject']['url'] != f'https://api.github.com/repos/{repo}/{kind}/{number}']
        self.notifications.append({
            'id': str(len(self.notifications) + 1), 'unread': True, 'reason': reason,
            'updated_at': gh_time(updated), 'last_read_at': None,
            'subject': {'title': title, 'type': 'PullRequest' if pull else 'Issue',
                        'url': f'https://api.github.com/repos/{repo}/{kind}/{number}',
                        'latest_comment_url': f'https://api.github.com/repos/{repo}/{kind}/{number}'},
            'repository': {'id': 1296269, 'full_name': repo, 'name': repo.split('/')[1]},
        })
        self.last_modified = gh_http_date(updated)

    def calls(self, method, path):
        return [record for record in self.requests if record['method'] == method and record['path'] == path]

    # transport -----------------------------------------------------------
    def reply(self, status, value, url, headers=None):
        import json as _json
        from lib.channels.github.client import GitHubResponse
        merged = {'date': gh_http_date(self.now), **(headers or {})}
        body = b'' if value is None else _json.dumps(value).encode('utf-8')
        return GitHubResponse(status, {key.lower(): str(item) for key, item in merged.items()}, body, url)

    def __call__(self, method, url, headers, body):
        import json as _json
        import re as _re
        from urllib.parse import parse_qsl, urlsplit
        parts = urlsplit(url)
        record = {'method': method, 'path': parts.path, 'query': dict(parse_qsl(parts.query)),
                  'headers': dict(headers), 'body': _json.loads(body) if body else None, 'url': url}
        self.requests.append(record)
        for responder in list(self.responders):
            answer = responder(record)
            if answer is not None:
                return answer
        user = self.users.get(headers.get('Authorization', '').removeprefix('Bearer '))
        if user is None:
            return self.reply(401, {'message': 'Bad credentials'}, url)
        path = parts.path
        if method == 'GET' and path == '/user':
            return self.reply(200, user, url)
        if method == 'GET' and path == '/notifications':
            return self._notifications(record)
        match = _re.fullmatch(r'/repos/([^/]+/[^/]+)/(issues|pulls)/(\d+)(/comments)?', path)
        if match:
            key = (match.group(1).lower(), int(match.group(3)))
            if match.group(4) is None and match.group(2) == 'issues' and method == 'GET':
                issue = self.issues.get(key)
                return self.reply(200, issue, url) if issue else self.reply(404, {'message': 'Not Found'}, url)
            if key not in self.issues:
                return self.reply(404, {'message': 'Not Found'}, url)
            if match.group(2) == 'issues' and method == 'POST':
                return self._create(key, record, user, url)
            rows = (self.comments if match.group(2) == 'issues' else self.review_comments).get(key, [])
            return self._page(record, rows)
        match = _re.fullmatch(r'/repos/([^/]+/[^/]+)/pulls/comments/(\d+)', path)
        if match and method == 'GET':
            for rows in self.review_comments.values():
                for row in rows:
                    if row['id'] == int(match.group(2)):
                        return self.reply(200, row, url)
        return self.reply(404, {'message': 'Not Found'}, url)

    def _page(self, record, rows, headers=None):
        import hashlib
        from urllib.parse import urlencode, urlsplit, urlunsplit
        query = record['query']
        if query.get('since'):
            rows = [row for row in rows if row['updated_at'] >= query['since']]
        per_page, page = int(query.get('per_page', 30)), int(query.get('page', 1))
        chunk = rows[(page - 1) * per_page: page * per_page]
        etag = '"%s"' % hashlib.sha1(repr(chunk).encode()).hexdigest()
        if record['headers'].get('If-None-Match') == etag:
            return self.reply(304, None, record['url'], {'etag': etag, **(headers or {})})
        extra = {'etag': etag, **(headers or {})}
        if page * per_page < len(rows):
            parts = urlsplit(record['url'])
            following = urlunsplit(parts._replace(query=urlencode({**query, 'page': page + 1})))
            extra['link'] = f'<{following}>; rel="next"'
        return self.reply(200, chunk, record['url'], extra)

    def _notifications(self, record):
        common = {'x-poll-interval': self.poll_interval}
        if record['headers'].get('If-Modified-Since') == self.last_modified:
            return self.reply(304, None, record['url'], common)
        rows = sorted(self.notifications, key=lambda item: item['updated_at'], reverse=True)
        if record['query'].get('all') != 'true':
            rows = [row for row in rows if row['unread']]
        return self._page(record, rows, {**common, 'last-modified': self.last_modified})

    def _create(self, key, record, user, url):
        self._ids += 1
        row = {'id': self._ids, 'body': record['body']['body'], 'user': user,
               'created_at': gh_time(self.now), 'updated_at': gh_time(self.now),
               'html_url': f'https://github.com/{key[0]}/issues/{key[1]}#issuecomment-{self._ids}',
               'issue_url': f'https://api.github.com/repos/{key[0]}/issues/{key[1]}'}
        self.comments.setdefault(key, []).append(row)
        self.posted.append(row)
        return self.reply(201, row, url)


@pytest.fixture
def vault(monkeypatch, tmp_path):
    """The credential vault redirected to a per-test store and key."""
    import lib.credentials_vault as module
    monkeypatch.setattr(module, '_STORE_PATH', tmp_path / 'credentials_vault.json')
    monkeypatch.setattr(module, '_KEY_PATH', tmp_path / '.credentials_vault.key')
    monkeypatch.setattr(module, '_fernet', None)
    return module


@pytest.fixture
def github():
    return FakeGitHub()


@pytest.fixture
def github_enabled(store, vault):
    """An enabled GitHub account; its token sits in the owner's reserved vault entry."""
    def configure(frontier=GH_NOW - 600, **extra):
        entry = f'channel.u{OWNER}.github.token'
        vault.set_entry(entry, GH_TOKEN)
        store.update_account('github', lambda account: {
            **account, 'enabled': True,
            'identity': {'uid': str(GH_USER['id']), 'mis': GH_USER['login'], 'name': GH_USER['name']},
            'credential': {'name': entry, 'managed': True}, 'frontierAtMs': frontier * 1000, **extra})
        return store.account('github')
    return configure


def github_adapter(store, fake, *, clock=None, owner=OWNER):
    """A prepared GitHubAdapter whose client talks to ``fake`` (no worker thread)."""
    from lib.channels.github.adapter import GitHubAdapter, api_base_url, token_source
    from lib.channels.github.client import GitHubClient

    def factory(owner_id, account):
        return GitHubClient(api_base_url(account), token_source(account), transport=fake,
                            clock=lambda: fake.now * 1000)
    adapter = GitHubAdapter(owner, store=store, client_factory=factory,
                            clock=clock or (lambda: fake.now * 1000))
    adapter.prepare()
    return adapter
