"""Linux memory-only child accounting through wait4, including very short lives.

Entry: observe_children() in an isolated benchmark worker. Popen's existing wait
lock/exit state machine stays authoritative; only its waitpid calls use wait4.
No sampler, helper process or timing-cohort instrumentation is introduced.
"""
from contextlib import contextmanager
import os
import subprocess

from benchmarks.repository_index.record_journal import RecordJournal


class ChildMemoryCapture:
    """Completed scalar receipts materialize after the observed worker returns."""
    def __init__(self, journal):
        self.journal = journal
        self.started = 0
        self.failure = None
        self.rows = None
        self.journal_receipt = None

    def record_exit(self, row):
        if self.failure is not None:
            return
        try:
            self.journal.append(row)
        except Exception as error:
            # wait4 already reaped the child. Let Popen commit its real exit
            # status before refusing the observation at the capture boundary.
            self.failure = error

    def finish(self):
        if self.failure is not None:
            raise self.failure
        if self.journal.count != self.started:
            raise AssertionError('child memory observation has no exact joined exit')
        self.rows = child_receipts(self.journal.read())
        self.journal_receipt = self.journal.receipt()


@contextmanager
def observe_children():
    original = subprocess.Popen
    journal = RecordJournal()
    children = ChildMemoryCapture(journal)

    class ObservedProcess(original):
        def __init__(self, *args, **kwargs):
            self.memory_receipt = {}
            super().__init__(*args, **kwargs)
            self.memory_receipt['pid'] = self.pid
            children.started += 1

        def _observe_wait(self, pid, flags):
            joined, status, usage = os.wait4(pid, flags)
            if joined:
                if 'returncode' in self.memory_receipt:
                    raise AssertionError('child memory receipt collected twice')
                self.memory_receipt.update(pid=joined,
                    returncode=os.waitstatus_to_exitcode(status),
                    peak_rss_bytes=usage.ru_maxrss * 1024,
                    user_cpu_seconds=usage.ru_utime, system_cpu_seconds=usage.ru_stime)
                children.record_exit(self.memory_receipt)
            return joined, status

        def _try_wait(self, flags):
            return self._observe_wait(self.pid, flags)

        def _internal_poll(self, _deadstate=None):
            return super()._internal_poll(_deadstate, _waitpid=self._observe_wait)

    with journal:
        subprocess.Popen = ObservedProcess
        try:
            yield children
            children.finish()
        finally:
            subprocess.Popen = original


def child_receipts(children):
    """Missing exit evidence is an invalid observation, never a zero-memory child."""
    if isinstance(children, ChildMemoryCapture):
        if children.rows is None:
            raise RuntimeError('child memory capture is not complete')
        children = children.rows
    result = []
    for row in children:
        if (type(row.get('pid')) is not int or type(row.get('returncode')) is not int
                or type(row.get('peak_rss_bytes')) is not int or row['peak_rss_bytes'] < 0):
            raise AssertionError('child memory observation has no exact joined exit')
        result.append(dict(row))
    if len({row['pid'] for row in result}) != len(result):
        raise AssertionError('child PIDs reused within memory observation')
    return result
