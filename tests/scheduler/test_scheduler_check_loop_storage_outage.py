"""A storage outage in the scheduler check loop is logged once per tick, briefly.

The check loop retries every 30 s. A fenced authority answers every tick with
the same availability failure, so a full ERROR traceback per tick buried the
storage runtime's own verdict under thousands of identical stacks.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('retryable', [True, False])
def test_storage_outage_is_a_warning_without_traceback(monkeypatch, caplog, retryable):
    from lib.identity import PrincipalContext
    from lib.scheduler import manager as module

    manager = module.ScheduledTaskManager()
    ticked = threading.Event()

    def fenced_tick():
        ticked.set()
        raise StorageError(
            'database_unavailable', 'Storage runtime recovery is stopped',
            retryable=retryable)

    monkeypatch.setattr(manager, '_check_and_run_due_tasks', fenced_tick)
    principal = PrincipalContext.system(subject_id='scheduler-test', owner_user_id=7,
                                        scopes={'scheduler:run'})
    with caplog.at_level(logging.WARNING, logger='lib.scheduler.manager'):
        try:
            assert manager.start(principal=principal)
            assert ticked.wait(5)
        finally:
            assert manager.stop(timeout=5)

    records = [record for record in caplog.records
               if record.name == 'lib.scheduler.manager'
               and 'check loop' in record.getMessage()]
    assert len(records) == 1, [record.getMessage() for record in records]
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is None
