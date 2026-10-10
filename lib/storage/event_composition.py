"""Pure native-advertised event composition costs, separate from admission.

The engine owns format byte constants. This module validates its cached
readiness declaration and estimates one independent raw stream/catalog write.
It performs no RPC, source read, backend selection or durability validation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from tofu_db.generated_v2 import NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT


@dataclass(frozen=True, slots=True)
class EventBatchCompositionBudget:
    maximum_carried_payload_bytes: int
    transaction_headroom_bytes: int
    stream_segment_header_bytes: int
    stream_event_header_bytes: int
    event_envelope_bytes: int
    metadata_base_bytes: int
    metadata_task_id_copies: int
    metadata_type_copies: int

    @property
    def composition_byte_capacity(self) -> int:
        return self.maximum_carried_payload_bytes - self.transaction_headroom_bytes

    def estimate(self, raw_event_json_bytes: int, task_id_bytes: int,
                 event_type_bytes: int, logical_type_bytes: int) -> int:
        return (raw_event_json_bytes + self.event_envelope_bytes
                + self.stream_segment_header_bytes + task_id_bytes
                + self.stream_event_header_bytes + event_type_bytes
                + self.metadata_base_bytes
                + self.metadata_task_id_copies * task_id_bytes
                + self.metadata_type_copies * logical_type_bytes)


def native_event_batch_composition_budget(value: object) -> EventBatchCompositionBudget | None:
    """Absent/unknown/malformed declarations keep legacy wire grouping.

    Compatibility does not claim spill avoidance. A declaration only changes
    optional grouping; the native engine still owns transaction admission.
    """
    if not isinstance(value, Mapping) or value.get('contract') != NATIVE_EVENT_BATCH_COMPOSITION_CONTRACT:
        return None
    fields = ('maximumCarriedPayloadBytes', 'transactionHeadroomBytes',
              'streamSegmentHeaderBytes', 'streamEventHeaderBytes',
              'eventEnvelopeBytes', 'metadataBaseBytes',
              'metadataTaskIdCopies', 'metadataTypeCopies')
    numbers = [value.get(field) for field in fields]
    if any(type(number) is not int or not 0 <= number <= (1 << 63) - 1 for number in numbers):
        return None
    result = EventBatchCompositionBudget(*numbers)
    if (result.maximum_carried_payload_bytes <= result.transaction_headroom_bytes
            or result.stream_segment_header_bytes == 0
            or result.stream_event_header_bytes == 0 or result.metadata_base_bytes == 0):
        return None
    return result
