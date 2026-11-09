"""Attempt-local text accumulation without copying the prefix for every delta.

Entry point: StreamTextBuffer, borrowed by SSEAccumulator for content, reasoning
and opaque signatures. Equal-depth adjacent fragments merge like binary carry;
no stream policy, text normalization, size threshold or external dependency.
"""


class StreamTextBuffer:
    """Preserve exact strings with logarithmically many pending fragments.

    Appends copy each fragment at most once per merge depth. Explicit reads
    materialize a string and release its fragments; a later append cannot
    mutate an already returned snapshot. The accumulator owns this lifetime.
    """

    __slots__ = ('_parts',)

    def __init__(self, text: str = ''):
        self._parts: list[tuple[int, str]] = []
        self.append(text)

    def __bool__(self) -> bool:
        return bool(self._parts)

    def append(self, text: str) -> None:
        if not text:
            return
        depth = 0
        end = len(self._parts)
        while end and self._parts[end - 1][0] == depth:
            text = self._parts[end - 1][1] + text
            end -= 1
            depth += 1
        # Publish only after every concatenation succeeds. A failed allocation
        # must leave the same accepted prefix available to error recovery.
        self._parts[end:] = [(depth, text)]

    def getvalue(self) -> str:
        if not self._parts:
            return ''
        if len(self._parts) > 1:
            text = ''.join([part for _, part in self._parts])
            depth = self._parts[0][0] + 1
            self._parts[:] = [(depth, text)]
        return self._parts[0][1]
