"""Pure SSE event assembly shared by sync and async SDK readers.

The line framer owns UTF-8/byte limits. This decoder publishes JSON objects only
at an empty-line boundary; incomplete final frames are discarded per SSE.
"""
from __future__ import annotations

import json

from ._protocol import parse_sequence, task_terminal


class SSEEventDecoder:
    def __init__(self):
        self.event_name = ''
        self.event_id: int | None = None
        self.data_lines: list[str] = []
        self.stopped = False

    def feed(self, line: str) -> dict | None:
        if self.stopped or line.startswith(':'):
            return None
        if line:
            field, separator, value = line.partition(':')
            if separator:
                if field == 'data':
                    self.data_lines.append(value.strip())
                elif field == 'event':
                    self.event_name = value.strip()
                elif field == 'id':
                    self.event_id = parse_sequence(value)
            return None

        data = '\n'.join(self.data_lines)
        name, sequence = self.event_name, self.event_id
        self.event_name, self.event_id, self.data_lines = '', None, []
        if not data:
            return None
        if data == '[DONE]':
            self.stopped = True
            return None
        try:
            event = json.loads(data)
        except (ValueError, RecursionError):
            return None
        if not isinstance(event, dict):
            return None
        if name:
            event.setdefault('event', name)
        if sequence is not None:
            event.setdefault('seq', sequence)
        self.stopped = task_terminal(event) or event.get('event') == 'message_stop'
        return event
