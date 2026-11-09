"""Conformance drivers: each adapter against its platform fake, through the protocol.

``reference`` and ``reference-stdio`` run the protocol-native reference adapter
in-process and in a child process over the stdio binding; ``dx``, ``github``,
``wecom``, ``qq`` and ``wechat`` run today's adapters unchanged behind the
compatibility shim (``harness.Shim``) against the platform fakes the channel
suite already uses (``tests/channels/bot_fakes.py``, ``conftest.FakeGitHub``),
extended here with the platform behaviours the cases need.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from lib.channels.model import ThreadKey
from lib.channels.protocol import codec
from lib.channels.protocol.host import AdapterHost, InProcessLink
from lib.channels.store import now_ms
from tests.channels.conftest import OWNER
from tests.channels.conformance.harness import Driver, Msg, NotApplicable, advance_core_clock, eventually
from tests.channels.conformance.reference import (
    BOT_ID, CHANNEL as REF, GROUP_ID, OTHER_ID, OWNER_ID, RATE_LIMIT_RETRY_AFTER_MS, ReferenceAdapter,
    ReferencePlatform, install_reference_channel,
)

_ROOT = Path(__file__).resolve().parents[3]


# ── the reference adapter ─────────────────────────────────────────────────

class ReferenceDriver(Driver):
    """The protocol-native adapter in-process: every failure is a core gap."""

    name = 'reference'
    channel = REF

    def setup(self) -> None:
        from lib.channels.bots import ids
        install_reference_channel(self.env.monkeypatch)
        self.command = ids.thread_key(REF, 'uid', OWNER_ID)
        self.group = ids.thread_key(REF, 'gid', GROUP_ID)
        self.store.update_account(REF, lambda account: {
            **account, 'enabled': True, 'identity': {'uid': OWNER_ID, 'name': 'Owner'},
            'commandChat': str(self.command), 'frontierAtMs': 1})
        self.store.update_thread(self.group, lambda value: {**value, 'mode': 'watch', 'title': 'Team'})
        self.platform = ReferencePlatform()
        self._open()

    def _open(self) -> None:
        self.adapter = ReferenceAdapter(self.platform)
        self.host = AdapterHost(OWNER, REF, InProcessLink(self.adapter), store=self.store)
        self.host.open()

    def teardown(self) -> None:
        self.host.close(2.0)

    def control(self, name: str, **params) -> Any:
        return self.adapter.control(name, params)

    def connect(self) -> None:
        eventually(lambda: any(health['state'] == 'connected' for health in self.host.health_log),
                   what='the adapter reporting connected')
        self.settle()

    def settle(self) -> None:
        eventually(lambda: self.control('idle')['idle'] and not self.host.catch_up.busy,
                   what='the adapter delivering its queue and the core catching up')

    def tick(self) -> None:
        self.host.tick()

    def execute(self) -> None:
        from lib.channels import outbox
        try:
            outbox.execute(OWNER, self.host.sender(), channel=REF, store=self.store)
        except Exception as error:  # an executor logs a failed pass and keeps running
            self.last_execute_error = error

    def alive(self) -> bool:
        return True

    def command_thread(self) -> ThreadKey:
        return self.command

    def watched_thread(self) -> ThreadKey:
        return self.group

    def _post(self, thread: ThreadKey, text: str, *, sender: str = OTHER_ID, kind: str = 'user',
              deliver: bool = True) -> Msg:
        ref = codec.thread_ref(thread)
        info = self.control('post', kind=ref['kind'], threadId=ref['id'], sender=sender, senderKind=kind,
                            text=text, deliver=deliver)
        return Msg(thread, text, info['id'], {'seq': info['seq']})

    def post(self, thread: ThreadKey, text: str) -> Msg:
        return self._post(thread, text)

    def redeliver(self, msg: Msg) -> None:
        self.control('deliver', id=msg.platform_id)

    def pair(self, thread: ThreadKey) -> tuple[Msg, Msg]:
        return (self._post(thread, 'older ' + os.urandom(4).hex(), deliver=False),
                self._post(thread, 'newer ' + os.urandom(4).hex(), deliver=False))

    def deliver(self, msg: Msg) -> None:
        self.control('deliver', id=msg.platform_id)

    def edit(self, msg: Msg, text: str) -> None:
        self.control('edit', id=msg.platform_id, text=text)

    def recall(self, msg: Msg) -> None:
        self.control('recall', id=msg.platform_id)

    def bot_post(self, text: str) -> Msg:
        return self._post(self.group, text, sender=BOT_ID, kind='bot')

    def post_unseen(self, thread: ThreadKey, text: str) -> Msg:
        msg = self._post(thread, text, deliver=False)
        # Missed while the live stream was down, so sent before the reconnect: let the clock move past it.
        sent_at = int(msg.extra['seq'][:13])
        eventually(lambda: now_ms() > sent_at, what='time passing the missed message')
        return msg

    def fail_next_history(self) -> None:
        self.control('set', history_failures=1)

    def report(self, state: str, code: str) -> None:
        """The adapter reports a transition; a notification, so wait until the core recorded it (the
        core may record its own health right after, a catch-up's outcome)."""
        health = {'state': state, 'code': code, 'atMs': now_ms()}
        self.control('health', health=health)
        eventually(lambda: health in self.host.health_log, what='the core recording the health report')

    def reconnect(self) -> None:
        # The adapter reports the transport loss and its return; catching up is the core's (P3).
        self.report('disconnected', 'transport_lost')
        self.report('connected', 'ok')

    def received(self, thread: ThreadKey) -> list[str]:
        ref = codec.thread_ref(thread)
        return self.control('received', kind=ref['kind'], threadId=ref['id'])['texts']

    def lose_next_answer(self) -> None:
        self.control('set', lose_next=True)

    def send_threads(self) -> tuple[ThreadKey, ThreadKey]:
        self.store.update_thread(self.group, lambda value: {**value, 'replyPolicy': 'auto'})
        return self.command, self.group

    def fail_reconciliation_reads(self, thread: ThreadKey) -> None:
        # The reference platform fails its history reads by count; no other read happens in these windows.
        self.control('set', history_failures=1_000_000)

    def heal_reconciliation_reads(self, thread: ThreadKey) -> None:
        self.control('set', history_failures=0)

    def deliver_without_receipt(self, item) -> None:
        self.claim(item)
        self.control('deliver_without_receipt', thread=codec.thread_ref(ThreadKey.parse(item.thread)),
                     text=item.text, marker=item.id)

    def restart(self) -> None:
        self.host.close(2.0)
        self._open()

    def rate_limit_sends(self) -> None:
        self.control('set', rate_limited=True)

    def end_rate_limit(self) -> None:
        self.control('set', rate_limited=False)
        advance_core_clock(self.env, RATE_LIMIT_RETRY_AFTER_MS + 1)

    def attempts(self, item) -> int:
        return self.control('attempts', key=item.id)['count']

    def echo(self, item) -> None:
        self.control('echo', thread=codec.thread_ref(ThreadKey.parse(item.thread)), marker=item.id)

    def self_send_outside_outbox(self, text: str) -> Msg:
        ref = codec.thread_ref(self.command)
        info = self.control('post', kind=ref['kind'], threadId=ref['id'], sender=OWNER_ID, text=text,
                            marker='tool-' + os.urandom(4).hex())
        return Msg(self.command, text, info['id'])

    def expire_auth(self) -> None:
        self.report('auth_required', 'credential_rejected')

    def runtime_fault(self) -> None:
        self.report('runtime_missing', 'runtime_unavailable')

    def displace(self) -> None:
        self.report('displaced', 'superseded')

    def network_fault(self) -> None:
        self.control('set', network_down=True)
        self.report('disconnected', 'network_unreachable')


class ReferenceStdioDriver(ReferenceDriver):
    """The same adapter in a child process over the stdio binding (JSON-RPC 2.0 lines)."""

    name = 'reference-stdio'

    def _open(self) -> None:
        from lib.channels.protocol.stdio import StdioLink
        env = {**os.environ, 'PYTHONPATH': str(_ROOT)}
        self.link = StdioLink([sys.executable, '-m', 'tests.channels.conformance.reference'], env=env, cwd=str(_ROOT))
        self.host = AdapterHost(OWNER, REF, self.link, store=self.store)
        self.host.open()

    def control(self, name: str, **params) -> Any:
        return self.link.request(f'test/{name}', params)

    def restart(self) -> None:
        raise NotApplicable('the stdio reference platform lives in the adapter process')


def _drivers() -> dict[str, type[Driver]]:
    from tests.channels.conformance.driver_bots import QQDriver, WeChatDriver, WeComDriver
    from tests.channels.conformance.driver_dx import DxDriver
    from tests.channels.conformance.driver_github import GitHubDriver
    return {'reference': ReferenceDriver, 'reference-stdio': ReferenceStdioDriver, 'dx': DxDriver,
            'github': GitHubDriver, 'wecom': WeComDriver, 'qq': QQDriver, 'wechat': WeChatDriver}


DRIVERS: dict[str, type[Driver]] = _drivers()

__all__ = ['DRIVERS']
