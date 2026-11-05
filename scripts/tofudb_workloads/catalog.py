"""Ordered storage comparison phases; adapters and measurement policy stay in the CLI owner."""
from scripts.tofudb_workloads.concurrency import ConcurrentWorkloads
from scripts.tofudb_workloads.history import HistoryWorkloads
from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts
from scripts.tofudb_workloads.records import RecordWorkloads
from scripts.tofudb_workloads.recovery import RecoveryWorkloads
from scripts.tofudb_workloads.search import SearchWorkload


def build_workloads(inputs: WorkloadInputs, ports: WorkloadPorts, search_word: str):
    records = RecordWorkloads(inputs, ports)
    history = HistoryWorkloads(inputs, ports)
    search = SearchWorkload(inputs, ports, search_word)
    concurrency = ConcurrentWorkloads(inputs, ports, records)
    recovery = RecoveryWorkloads(inputs, ports, records)
    turn_append_setup, turn_append_op = records.make_turn_append('main')
    return [
        ('record_put_burst', None, records.record_put_op, inputs.scale['writes']),
        ('record_get_points', records.record_put_setup, records.record_get_op, inputs.scale['reads']),
        ('conversation_create_burst', None, records.conv_create_op, inputs.scale['writes']),
        ('sidebar_list_page', records.conv_seed_setup('sb'), records.conv_list_op,
         inputs.scale['reads']),
        ('conversation_open', records.conv_seed_setup('op'), records.conv_get_op,
         inputs.scale['reads']),
        ('turn_append_burst', turn_append_setup, turn_append_op,
         inputs.scale['turn_writes']),
        ('conversation_search', search.search_setup, search.search_op, inputs.scale['search_reads']),
        ('mixed_80_10_10', records.mixed_setup, records.mixed_op, inputs.scale['mixed']),
        ('open_windowed_long_history', history.history_open_setup, history.history_open_op,
         inputs.scale['reads']),
        ('sidebar_with_history', history.sidebar_history_setup, history.sidebar_history_op,
         inputs.scale['reads']),
        ('transcript_bulk_hydration', history.hydration_setup, history.hydration_op,
         max(10, inputs.scale['reads'] // 10)),
        ('concurrent_read', None, None, inputs.scale['concurrent_reads'],
         concurrency.concurrent_read_runner),
        ('swarm_write', None, None, inputs.scale['swarm_writes_each'],
         concurrency.swarm_write_runner),
        ('turn_append_large', history.large_turn_setup, history.large_turn_op,
         inputs.scale['large_turn_writes']),
        ('maintenance_reopen', recovery.maintenance_setup, None,
         inputs.scale['maintenance_rounds'], recovery.maintenance_reopen_runner),
        ('crash_recovery', recovery.crash_setup, recovery.crash_op,
         inputs.scale['maintenance_rounds'], recovery.crash_recovery_runner),
    ]
