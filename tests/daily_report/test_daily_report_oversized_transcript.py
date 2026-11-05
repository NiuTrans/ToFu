"""Daily reports over transcripts too large for one storage response.

A conversation whose whole transcript the storage authority refuses is read
backward in bounded windows to the report day's start, and must produce the
digest a full hydration would. A day whose transcripts cannot be read is an
extraction failure: never an empty day, never a saved report.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from lib.conversations.repository import ConversationRepository
from lib.daily_report import conversations, generator, scheduler, storage
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41
REPORT_DAY = dt.date(2026, 8, 2)

_RESPONSE_REFUSALS = {
    'sql': lambda: StorageError(
        'database_protocol_error', 'Storage frame exceeds the size limit'),
    'native': lambda: StorageError(
        'database_resource_exhausted', 'database_resource_exhausted'),
    # Engines predating the non-retryable domain_resource_bound refusal.
    'native_legacy': lambda: StorageError(
        'database_unavailable', 'database_resource_exhausted', True),
}


def _at(day: dt.date, hour: int, minute: int = 0) -> int:
    return int(dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
               * 1000)


def _message(role, text, timestamp, *, size=1, tools=()):
    message = {'role': role, 'content': text, 'timestamp': timestamp,
               'size': size}
    if tools:
        message['toolRounds'] = [{'calls': [{'name': name} for name in tools]}]
    return message


def _transcripts(day: dt.date) -> dict[str, list[dict]]:
    previous, following = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
    return {
        'ordinary': [
            _message('user', 'old ordinary question', _at(previous, 9)),
            _message('user', 'ordinary question', _at(day, 10)),
            _message('assistant', 'ordinary answer', _at(day, 10, 1),
                     tools=['read_file']),
        ],
        'oversized': [
            _message('user', 'yesterday question', _at(previous, 9)),
            _message('assistant', 'yesterday answer', _at(previous, 9, 5),
                     size=6),
            _message('user', 'today question', _at(day, 9)),
            _message('assistant', 'today answer', _at(day, 9, 5), size=6,
                     tools=['run_command']),
            _message('user', 'untimestamped follow-up', 0),
            _message('assistant', 'evening answer', _at(day, 18)),
            _message('user', 'tomorrow question', _at(following, 8)),
        ],
    }


class _Storage:
    """Both backends' list/window contract under a response byte budget."""

    def __init__(self, transcripts, *, budget, refusal='native', day):
        self.transcripts = transcripts
        self.budget = budget
        self.refusal = _RESPONSE_REFUSALS[refusal]
        self.updated_at = _at(day, 18)
        self.accepted_windows = []

    def _document(self, conversation_id, messages):
        return {
            'metadata': {
                'id': conversation_id, 'user_id': OWNER_USER_ID,
                'title': f'Title {conversation_id}', 'settings': {},
                'created_at': self.transcripts[conversation_id][0]['timestamp'],
                'updated_at': self.updated_at,
                'msg_count': len(self.transcripts[conversation_id]), 'rev': 1,
            },
            'messages': list(messages),
        }

    def _admit(self, messages):
        if sum(message['size'] for message in messages) > self.budget:
            raise self.refusal()

    def query(self, operation, payload):
        assert payload['user_id'] == OWNER_USER_ID
        if operation == 'conversation.list':
            if payload.get('ids') is None:
                return [self._document(conversation_id, [])
                        for conversation_id in self.transcripts]
            documents = [
                self._document(conversation_id,
                               self.transcripts[conversation_id])
                for conversation_id in payload['ids']
            ]
            self._admit([message for document in documents
                         for message in document['messages']])
            return documents
        assert operation == 'conversation.get'
        messages = self.transcripts[payload['conv_id']]
        before = payload.get('before_sequence')
        end = len(messages) if before is None else min(before, len(messages))
        start = max(0, end - payload['message_window'])
        self._admit(messages[start:end])
        self.accepted_windows.append((start, end))
        document = self._document(payload['conv_id'], messages[start:end])
        document['message_page'] = {
            'total_count': len(messages), 'start': start, 'end': end,
        }
        return document


def _use_storage(monkeypatch, client):
    repository = ConversationRepository(lambda *, write=False: client)
    monkeypatch.setattr(
        'lib.conversations.repository.scan_conversations_bounded',
        lambda **kwargs: repository.scan_bounded(**kwargs))


@pytest.fixture
def report_store(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, '_REPORTS_DIR', str(tmp_path))
    monkeypatch.setattr(storage, '_active_jobs', {})
    monkeypatch.setattr(
        storage, '_invalidate_calendar',
        lambda _date, *, owner_user_id: None)
    return tmp_path


@pytest.mark.parametrize('refusal', sorted(_RESPONSE_REFUSALS))
def test_oversized_conversation_digest_matches_full_hydration(
        monkeypatch, refusal):
    unlimited = _Storage(_transcripts(REPORT_DAY), budget=10**9,
                         day=REPORT_DAY)
    _use_storage(monkeypatch, unlimited)
    expected = conversations._extract_convs_for_date(
        REPORT_DAY.isoformat(), owner_user_id=OWNER_USER_ID)

    limited = _Storage(_transcripts(REPORT_DAY), budget=8, refusal=refusal,
                       day=REPORT_DAY)
    _use_storage(monkeypatch, limited)
    got = conversations._extract_convs_for_date(
        REPORT_DAY.isoformat(), owner_user_id=OWNER_USER_ID)

    assert got == expected
    oversized = next(digest for digest in got if digest['id'] == 'oversized')
    for in_day in ('today question', 'today answer', 'untimestamped follow-up',
                   'evening answer'):
        assert in_day in oversized['transcript']
    for out_of_day in ('yesterday', 'tomorrow question'):
        assert out_of_day not in oversized['transcript']
    assert oversized['rounds'] == 2
    assert oversized['toolsUsed'] == ['run_command']
    # Windows stop at the newest message before the day: message 0 is
    # never read, while full hydration read every message.
    assert min(start for start, _ in limited.accepted_windows) == 1


def test_unreadable_oversized_message_fails_extraction_not_empty_day(
        monkeypatch):
    transcripts = _transcripts(REPORT_DAY)
    transcripts['oversized'][5]['size'] = 9
    _use_storage(monkeypatch, _Storage(transcripts, budget=8, day=REPORT_DAY))

    with pytest.raises(StorageError) as caught:
        conversations._extract_convs_for_date(
            REPORT_DAY.isoformat(), owner_user_id=OWNER_USER_ID)
    assert caught.value.message == 'database_resource_exhausted'


def test_scheduled_backfill_records_extraction_failure_and_saves_nothing(
        monkeypatch, report_store, caplog):
    from lib.scheduler.manager import ScheduledTaskManager

    yesterday = dt.date.today() - dt.timedelta(days=1)
    transcripts = _transcripts(yesterday)
    transcripts['oversized'][5]['size'] = 9
    _use_storage(monkeypatch, _Storage(transcripts, budget=8, day=yesterday))
    monkeypatch.setattr(
        scheduler, '_analyse_conversations',
        lambda *_args, **_kwargs: pytest.fail('a failed day must not be analysed'))
    principal = PrincipalContext.system(
        subject_id='daily-report-test', owner_user_id=OWNER_USER_ID,
        scopes={'reports:maintain'})

    with caplog.at_level(logging.INFO, logger='lib.daily_report'):
        summary = scheduler._backfill_yesterday_if_missing(principal=principal)

    assert summary['ok'] is False
    assert summary['status'] == 'extraction_failed'
    assert summary['date'] == yesterday.isoformat()
    assert 'database_resource_exhausted' in summary['error']
    assert storage._load_report(
        yesterday.isoformat(), owner_user_id=OWNER_USER_ID) is None
    assert not any('No conversations found' in record.getMessage()
                   for record in caplog.records)
    assert any(record.levelno == logging.ERROR
               and 'extraction failed' in record.getMessage()
               for record in caplog.records)

    manager = ScheduledTaskManager.__new__(ScheduledTaskManager)
    ok, result = manager._execute_task({
        'id': 'daily-report-task', 'user_id': OWNER_USER_ID,
        'task_type': 'daily_report_backfill', 'command': 'ignored',
    })
    assert ok is False
    assert json.loads(result)['status'] == 'extraction_failed'


def test_background_generation_reports_extraction_failure_without_saving(
        monkeypatch, report_store):
    previous = (REPORT_DAY - dt.timedelta(days=1)).isoformat()
    storage._save_report(previous, {
        'streams': [],
        'tomorrow': [{'id': 'todo-1', 'text': 'carry me', 'done': False}],
    }, owner_user_id=OWNER_USER_ID)
    transcripts = _transcripts(REPORT_DAY)
    transcripts['oversized'][5]['size'] = 9
    _use_storage(monkeypatch, _Storage(transcripts, budget=8, day=REPORT_DAY))

    generator._generate_in_background(
        REPORT_DAY.isoformat(), True, owner_user_id=OWNER_USER_ID)

    job = storage._get_job(REPORT_DAY.isoformat(), owner_user_id=OWNER_USER_ID)
    assert job['status'] == 'error'
    assert 'database_resource_exhausted' in job['error']
    assert storage._load_report(
        REPORT_DAY.isoformat(), owner_user_id=OWNER_USER_ID) is None
