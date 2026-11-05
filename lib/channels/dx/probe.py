"""Read-only live probe of the 大象 bridge (docs/CHANNELS.md Verification).

Responsibility: prove, against the owner's real account, that the launcher
authenticates, ``whoami`` resolves, the persistent IM handlers receive pushes
while no command is in flight, and history/sessions answer — printing counts
only, never names or message content. It issues only ``whoami``, ``sessions``
and ``history``: no send, group creation, read marking or other write.

Usage: ``python -m lib.channels.dx.probe --owner 1 [--listen-seconds 20]``.
"""
from __future__ import annotations

import argparse
import json
import queue
import time

from lib.channels.dx.adapter import (
    DxBridge, _argv, _child_environment, event_from_history, event_from_live, history_pages,
    launch_spec, svr_time_from_mid, thread_from_session,
)

READ_ONLY_OPS = frozenset({'whoami', 'sessions', 'history'})
# Pages read per session kind: enough to prove the walk moves strictly older.
PROBE_HISTORY_PAGES = 2


class _ReadOnly:
    """Bridge view that refuses every operation except reads."""

    def __init__(self, bridge: DxBridge):
        self._bridge = bridge

    def request(self, op: str, **arguments):
        if op not in READ_ONLY_OPS:
            raise ValueError(f'probe refuses non-read operation {op}')
        return self._bridge.request(op, **arguments)


def _history_report(reader: _ReadOnly, thread) -> dict:
    pages = []
    for rows in history_pages(reader, thread):
        pages.append([e for e in (event_from_history(r, thread) for r in rows) if e])
        if len(pages) == PROBE_HISTORY_PAGES:
            break
    events = [e for page in pages for e in page]
    ids = [int(e.external_id) for e in events]
    return {
        'pages': len(pages), 'normalized': len(events), 'distinct': len(set(ids)),
        'strictlyOlderPages': all(max(int(e.external_id) for e in later) < min(int(e.external_id) for e in earlier)
                                  for earlier, later in zip(pages, pages[1:]) if earlier and later),
        'withSender': sum(1 for e in events if e.sender_id),
        'withServerTime': sum(1 for e in events if svr_time_from_mid(e.external_id)),
        'withClientId': sum(1 for e in events if e.client_id),
        'withReplyFacts': sum(1 for e in events if e.reply_to_external_id),
        'withMentions': sum(1 for e in events if e.mentioned_ids or e.mentions_all),
    }


def probe(owner: int, listen_seconds: float) -> dict:
    spec = launch_spec(owner)
    report: dict = {'skillInstalled': spec.installed}
    if not spec.installed:
        return report
    bridge = DxBridge(_argv(spec), env=_child_environment(owner), cwd=spec.skill_dir)
    reader = _ReadOnly(bridge)
    counts = {'status': [], 'messages': 0, 'messagesBySource': {}}
    deadline = time.monotonic() + listen_seconds
    try:
        while time.monotonic() < deadline:
            try:
                item = bridge.events.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if item is None:
                break
            if item.get('type') == 'status':
                counts['status'].append(item.get('status'))
                if item.get('status') == 'connected':
                    whoami = reader.request('whoami')
                    report['whoamiOk'] = bool(whoami.get('ok') and (whoami.get('result') or {}).get('uid'))
                    now_ms = int(time.time() * 1000)
                    day_ms = 86_400_000
                    sessions = reader.request('sessions', startMs=now_ms - day_ms, endMs=now_ms, limit=500)
                    rows = (sessions.get('result') or {}).get('sessions') or []
                    report['sessionsLastDay'] = len(rows)
                    threads = [t for t in map(thread_from_session, rows) if t is not None]
                    report['sessionKinds'] = sorted({t.kind for t in threads})
                    report['history'] = {
                        kind: _history_report(reader, next(t for t in threads if t.kind == kind))
                        for kind in report['sessionKinds']}
            elif item.get('type') == 'message':
                counts['messages'] += 1
                if event_from_live(item.get('message') or {}, source=str(item.get('source')),
                                   self_uid='', quotes=item.get('quotes')) is not None:
                    counts['messagesNormalized'] = counts.get('messagesNormalized', 0) + 1
                source = str(item.get('source'))
                counts['messagesBySource'][source] = counts['messagesBySource'].get(source, 0) + 1
    finally:
        bridge.close(5)
    report.update(counts)
    report['exitCode'] = bridge.returncode
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--owner', type=int, required=True)
    parser.add_argument('--listen-seconds', type=float, required=True)
    args = parser.parse_args()
    print(json.dumps(probe(args.owner, args.listen_seconds), ensure_ascii=False))


if __name__ == '__main__':
    main()
