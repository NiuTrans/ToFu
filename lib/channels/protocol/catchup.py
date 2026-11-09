"""Core-owned catch-up of one adapter session (docs/CHANNEL_PROTOCOL.md P3, docs/CHANNELS.md C1).

Responsibility: replay what the live stream may have missed, through the
adapter's ``threads`` (conversations active since the account frontier, or the
core's known threads when the adapter lists none) and ``history`` (each
non-ignored thread's messages after its watermark, or after ``max(frontier,
thread.sinceMs)`` for a thread without one), ingested through the host's own
event path, so echo detection and authority are those of live events. A
catch-up is owed from every ``connected`` the adapter reports and runs on its
own worker, never on the reporting thread (a binding's dispatcher must not wait
on the adapter), and again at each supervisor tick while owed. A failed read
or ingest keeps the session and live delivery: health becomes ``degraded``
(``history_failed`` or ``ingest_failed``, its detail a code only, never
content), the catch-up stays owed, and live events ingested meanwhile do not
move watermarks. A completed catch-up moves the frontier to its own start and
restores ``connected``. No timer, retry count or backoff: the tick is the
cadence.

Entry point: ``CatchUp`` (owned by ``AdapterHost``: ``owe``, ``start``,
``tick``, ``stop``, ``join``, ``owed``, ``busy``). Dependencies: ``codec``
(thread keys), ``lib.channels.policy`` (thread modes), the host's store.
"""
from __future__ import annotations

import threading
from typing import Any

from lib.channels.model import ChannelError, ThreadKey
from lib.channels.policy import effective_mode
from lib.channels.protocol.codec import thread_key, thread_ref
from lib.log import get_logger

logger = get_logger(__name__)


class _Failed(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = code, detail


def _cause(error: BaseException) -> str:
    """A failure's code for health and logs; never its message, which may quote content."""
    code = getattr(error, 'code', None)
    return f'{type(error).__name__} {code}' if code is not None else type(error).__name__


class CatchUp:
    """The owed catch-up of one ``AdapterHost`` and its single worker."""

    def __init__(self, host):
        self.host = host
        self._lock = threading.Lock()
        self._owed = False
        self._pending = False
        self._running = False
        self._stopped = False
        self._generation = 0
        self._worker: threading.Thread | None = None

    @property
    def owed(self) -> bool:
        with self._lock:
            return self._owed

    @property
    def busy(self) -> bool:
        """A catch-up is requested or running."""
        with self._lock:
            return self._pending or self._running

    def owe(self) -> None:
        """A connection began: a catch-up is owed and requested (``busy`` from now on)."""
        with self._lock:
            self._owed = True
            self._generation += 1
            self._pending = not self._stopped

    def tick(self) -> None:
        """The supervisor's scheduler tick: run an owed catch-up again."""
        with self._lock:
            if not self._owed or self._stopped:
                return
            self._pending = True
        self.start()

    def start(self) -> None:
        with self._lock:
            if self._stopped or self._running or not self._pending:
                return
            self._running = True
            self._worker = threading.Thread(target=self._work, name=f'channel-{self.host.channel}-catch-up',
                                            daemon=True)
            worker = self._worker
        worker.start()

    def stop(self) -> None:
        with self._lock:
            self._stopped, self._pending = True, False

    def join(self, timeout: float) -> bool:
        worker = self._worker
        if worker is None or worker is threading.current_thread():
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def _work(self) -> None:
        while True:
            with self._lock:
                if self._stopped or not self._pending:
                    self._running = False
                    return
                self._pending = False
                generation = self._generation
            try:
                self._run(generation)
            except BaseException:
                with self._lock:
                    self._running = False
                raise

    def _run(self, generation: int) -> None:
        host = self.host
        started = host._now_ms()
        try:
            threads, replayed = self._replay(started)
        except _Failed as failure:
            with self._lock:
                if self._stopped:
                    return
            logger.warning('[Channels] %s catch-up failed owner=%s: %s; retried at the next tick',
                           host.channel, host.owner, failure.detail)
            host.record_health({'state': 'degraded', 'code': failure.code, 'detail': failure.detail,
                                'atMs': host._now_ms()})
            return
        with self._lock:
            if self._generation == generation:
                self._owed = False  # a connection that began meanwhile still owes its own
        last = host.last_health or {}
        if last.get('state') == 'degraded' and last.get('code') in ('history_failed', 'ingest_failed'):
            host.record_health({'state': 'connected', 'code': 'ok', 'atMs': host._now_ms()})
        logger.info('[Channels] %s catch-up completed owner=%s threads=%d replayed=%d took_ms=%d',
                    host.channel, host.owner, threads, replayed, host._now_ms() - started)

    def _replay(self, started: int) -> tuple[int, int]:
        host = self.host
        account = host.store.account(host.channel) or {}
        frontier = account.get('frontierAtMs')
        if frontier is None or not host.supports('history'):
            # Never caught up (enabling starts the frontier now, C1), or no history to replay.
            self._advance_frontier(started)
            return 0, 0
        threads = self._threads(int(frontier), started)
        replayed = 0
        for thread, ref in threads:
            record = host.store.thread(thread) or {}
            if effective_mode(thread, record, account) == 'ignore':
                continue
            query: dict[str, Any] = {'thread': ref}
            if record.get('watermark'):
                query['afterSeq'] = str(record['watermark'])
            else:
                query['sinceMs'] = max(int(frontier), int(record.get('sinceMs') or 0))
            events = self._read('history', query)['events']
            if events:
                try:
                    host.ingest_events({'events': events}, advance_watermark=True)
                except Exception as error:
                    raise _Failed('ingest_failed', f'replay failed: {_cause(error)}') from error
                replayed += len(events)
        self._advance_frontier(started)
        return len(threads), replayed

    def _threads(self, since_ms: int, until_ms: int) -> list[tuple[ThreadKey, dict[str, Any]]]:
        host = self.host
        if host.supports('threads'):
            refs = [entry['thread'] for entry in
                    self._read('threads', {'sinceMs': since_ms, 'untilMs': until_ms})['threads']]
        else:
            refs = [thread_ref(ThreadKey.parse(record['key'])) for record in host.store.threads(host.channel)
                    if record.get('key')]
        threads = []
        for ref in refs:
            try:
                threads.append((thread_key(host.channel, ref), ref))
            except ChannelError:
                logger.warning('[Channels] %s catch-up skipped a thread the adapter named with an unusable ID '
                               'owner=%s', host.channel, host.owner)
        return threads

    def _read(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.host.call(method, params)
        except Exception as error:
            raise _Failed('history_failed', f'{method} failed: {_cause(error)}') from error

    def _advance_frontier(self, started: int) -> None:
        self.host.store.update_account(self.host.channel, lambda account: {
            **account, 'frontierAtMs': max(int(account.get('frontierAtMs') or 0), started)})


__all__ = ['CatchUp']
