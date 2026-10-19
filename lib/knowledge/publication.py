"""Local file rollback evidence for one knowledge metadata publication.

KnowledgeRepository marks dispatch before entering a storage client. The store
may remove candidate files on an exceptional exit only while dispatch has never
started. A later failed retry cannot erase an earlier ambiguous commit. This
object owns no files, storage, retries, background work or process-global state.
"""


class KnowledgePublicationAttempt:
    __slots__ = ('_dispatch_started',)

    def __init__(self):
        self._dispatch_started = False

    @property
    def rollback_safe(self) -> bool:
        return not self._dispatch_started

    def begin_dispatch(self) -> None:
        self._dispatch_started = True
