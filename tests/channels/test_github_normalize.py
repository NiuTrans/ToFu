"""GitHub normalization (C8): thread keys, event IDs, mentions, markers, notifications."""
import pytest

from lib.channels.github import normalize
from lib.channels.github.client import (
    GitHubResponse, iso_seconds, parse_time_ms, rate_limit_resume_ms,
)
from lib.channels.model import ChannelError, ThreadKey, parse_event_id
from tests.channels.conftest import GH_ALICE, GH_NOW, GH_USER, FakeGitHub, gh_http_date, gh_time

pytestmark = pytest.mark.unit

IDENTITY = {'uid': str(GH_USER['id']), 'mis': GH_USER['login'], 'name': GH_USER['name']}


def test_github_thread_keys_are_one_lowercase_spelling_per_issue_or_pull_request():
    key = ThreadKey.parse('github:Octo-Org/Hello.World_2#12')
    assert str(key) == 'github:octo-org/hello.world_2#12'
    assert (key.channel, key.kind, key.repository, key.number) == ('github', 'issue', 'octo-org/hello.world_2', 12)
    assert ThreadKey.github('Octo-Org/Hello.World_2', 12) == key
    thread, external = parse_event_id('github:Octo-Org/Hello.World_2#12/1793000000200000000000000000042')
    assert (thread, external) == (key, '1793000000200000000000000000042')
    for bad in ('github:octo/..#1', 'github:octo/.#1', 'github:octo/repo#0', 'github:-octo/repo#1',
                'github:octo/repo', 'github:octo/re po#1', 'dx:issue:12'):
        with pytest.raises(ChannelError):
            ThreadKey.parse(bad)
    assert str(ThreadKey.parse('dx:gid:500')) == 'dx:gid:500'


def test_event_ids_are_unique_across_github_id_sequences_and_time_ordered():
    created = GH_NOW * 1000
    same_number = [normalize.external_id(created, kind, 77) for kind in (1, 2, 3)]
    assert len(set(same_number)) == 3 and all(len(value) == 31 for value in same_number)
    earlier_big_id = normalize.external_id(created - 1000, 2, 99_999_999_999)
    later_small_id = normalize.external_id(created, 3, 5)
    assert int(earlier_big_id) < int(later_small_id)


@pytest.mark.parametrize('body, expected', [
    ('@octo-owner please look', True),
    ('cc @OCTO-OWNER.', True),
    ('(@Octo-Owner)', True),
    ('mail octo-owner@example.com or me@octo-owner', False),
    ('`@Octo-Owner` is a handle', False),
    ('```\n@Octo-Owner in code\n```\nplain', False),
    ('team ping @Octo-Owner/reviewers', False),
    ('@Octo-Owner2 is someone else', False),
])
def test_mentions_follow_github_login_syntax_outside_code(body, expected):
    assert normalize.mentions(body, GH_USER['login']) is expected


def test_outbox_marker_round_trips_and_is_invisible_markdown():
    item = 'ob' + '0123456789abcdef' * 2
    body = 'Done.' + normalize.marker_suffix(item)
    assert body.endswith(f'<!-- tofuOutbox:{item} -->')
    assert normalize.outbox_marker(body) == item
    assert normalize.outbox_marker('<!-- tofuOutbox:not-an-item -->') == ''


def test_notifications_name_issue_and_pull_request_threads_only():
    fake = FakeGitHub()
    fake.notify('Octo/Repo', 12, title='Fix race', updated=GH_NOW, pull=True)
    fake.notify('Octo/Repo', 7, title='Bug', updated=GH_NOW)
    thread, title, is_pull = normalize.notification_thread(fake.notifications[0])
    assert (str(thread), title, is_pull) == ('github:octo/repo#12', 'Fix race', True)
    assert normalize.notification_thread(fake.notifications[1])[2] is False
    release = {**fake.notifications[0], 'subject': {'type': 'Release', 'title': 'v1',
                                                    'url': 'https://api.github.com/repos/octo/repo/releases/1'}}
    assert normalize.notification_thread(release) is None


def test_comment_and_opening_post_events_carry_protocol_facts():
    fake = FakeGitHub()
    issue = fake.issue('octo/repo', 12, title='Plan', created=GH_NOW - 100, pull=True,
                       body='@octo-owner which plan?')
    thread = ThreadKey.parse('github:octo/repo#12')
    opening = normalize.issue_event(issue, thread, IDENTITY)
    assert opening.text == 'Plan\n\n@octo-owner which plan?'
    assert opening.mentioned_ids == (IDENTITY['uid'],) and opening.url.endswith('/pull/12')
    parent = fake.comment('octo/repo', 12, user=GH_USER, body='use a lock', created=GH_NOW - 50, review=True)
    reply = fake.comment('octo/repo', 12, user=GH_ALICE, body='done', created=GH_NOW - 10, review=True,
                         reply_to=parent['id'])
    event = normalize.comment_event(reply, thread, kind=normalize.KIND_REVIEW_COMMENT, identity=IDENTITY,
                                    reply_to=normalize.comment_reference(parent, normalize.KIND_REVIEW_COMMENT))
    assert event.sender_id == str(GH_ALICE['id']) and event.sender_name == 'alice'
    assert event.reply_to_sender_id == IDENTITY['uid']
    assert event.reply_to_external_id == normalize.external_id((GH_NOW - 50) * 1000, 3, parent['id'])
    assert event.sent_at_ms == (GH_NOW - 10) * 1000 and event.message_type == 3
    assert normalize.comment_event({'id': 'x'}, thread, kind=2, identity=IDENTITY) is None


def test_time_and_rate_limit_rules():
    assert iso_seconds(GH_NOW * 1000 + 999) == gh_time(GH_NOW)
    assert parse_time_ms(gh_time(GH_NOW)) == GH_NOW * 1000

    def response(status, headers, message=''):
        import json
        return GitHubResponse(status, headers, json.dumps({'message': message}).encode(), 'u')
    now = GH_NOW * 1000
    assert rate_limit_resume_ms(response(403, {'retry-after': '30'}), now) == now + 30_000
    assert rate_limit_resume_ms(response(403, {'x-ratelimit-remaining': '0',
                                               'x-ratelimit-reset': str(GH_NOW + 900)}), now) == (GH_NOW + 900) * 1000
    # The reset is GitHub's clock: the wait is taken from its Date header, so a
    # local clock five minutes ahead still waits the full fifteen minutes.
    skewed = now + 300_000
    assert rate_limit_resume_ms(response(403, {'x-ratelimit-remaining': '0', 'date': gh_http_date(GH_NOW),
                                               'x-ratelimit-reset': str(GH_NOW + 900)}), skewed) == skewed + 900_000
    assert rate_limit_resume_ms(response(429, {}), now) == now + 60_000
    assert rate_limit_resume_ms(response(403, {}, 'You have exceeded a secondary rate limit'), now) == now + 60_000
    assert rate_limit_resume_ms(response(403, {}, 'Resource not accessible by integration'), now) is None
    assert rate_limit_resume_ms(response(404, {}), now) is None
