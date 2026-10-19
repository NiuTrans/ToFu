"""Push-specific launch policy and metrics for the shared JSON file owner."""
from tofu_workers.json_frames import os as os
from tofu_workers.json_frames import JsonFrameStore, StoredJsonFrame
from lib.agent_core.push_policy import resolve_push_budget

StoredPushFrame = StoredJsonFrame


class PushFrameStore(JsonFrameStore):
    def __init__(self, *, segment_bytes: int | None = None):
        budget = resolve_push_budget()
        super().__init__(segment_bytes=segment_bytes or budget.spool_segment_bytes,
                         reuse_workers=budget.worker_reuse_target,
                         thread_name_prefix='tofu-push-frame')

    def snapshot(self) -> dict:
        value = super().snapshot()
        return {'event_spool_files': value['event_spool_files'],
                'event_spool_bytes': value['event_spool_bytes']}
