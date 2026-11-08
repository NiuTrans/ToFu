"""Commit-owned worker and conversation hints, without notification history.

Adapters publish their transaction-local hints after COMMIT. The common wait
primitive holds no database connection; independent channels avoid cross-noise.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from lib.storage.generated_worker_control import FORMAT, CONVERSATION_FORMAT
from lib.storage_authority_sql.commit_watch import CommitWatch


@dataclass
class WorkerCommitHints:
    queue_changed: bool = False
    cancelled_tasks: set[str] = field(default_factory=set)
    conversation_changes: set[tuple[int, str]] = field(default_factory=set)
    publisher: Callable[[bool, set[str], set[tuple[int, str]]], None] | None = None

    def publish(self):
        if self.publisher is not None:
            self.publisher(self.queue_changed, self.cancelled_tasks, self.conversation_changes)


class WorkerControl(CommitWatch):
    def __init__(self):
        super().__init__(('queue', 'cancel'), format=FORMAT,
                         hint_key='cancelTasks', hint_generation='cancel')
        self.conversations = CommitWatch(('change',), format=CONVERSATION_FORMAT,
            hint_key='conversations', hint_generation='change',
            encode_hints=lambda scopes: [dict(userId=owner, conversationId=conversation)
                                        for owner, conversation in sorted(scopes)])

    def publish(self, queue_changed, cancelled_tasks, conversation_changes=()):
        changed = ({'queue'} if queue_changed else set()) | ({'cancel'} if cancelled_tasks else set())
        self.publish_changes(changed, cancelled_tasks)
        self.conversations.publish_changes({'change'} if conversation_changes else set(), conversation_changes)

    def close(self):
        super().close()
        self.conversations.close()
