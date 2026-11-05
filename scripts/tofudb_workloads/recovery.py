"""Graceful reopen and crash controls; require exact acknowledged state before timing.

Entry: RecoveryWorkloads; dependencies are explicit inputs and execution ports.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
import json

from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts
from scripts.tofudb_workloads.records import RecordWorkloads


@dataclass(frozen=True)
class RecoveryWorkloads:
    inputs: WorkloadInputs
    ports: WorkloadPorts
    records: RecordWorkloads

    def maintenance_setup(self, engine):
        """Seed restart-worthy state: point records plus one turn history.

        The conversation gets its own identity: reusing the turn-append
        workload's conversation collides with it inside one shared authority.
        """
        self.records.record_put_setup(engine)
        engine.execute('conversation.create', {
            'conv_id': f'{self.inputs.namespace}_maint', 'user_id': self.inputs.user,
            'title': 'Maintenance reopen target',
            'settings': {},
            'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
        }, command=True)
        for i in range(self.inputs.scale['turn_writes']):
            at = self.inputs.search_timestamp + 120_000 + i
            engine.execute('turn.append_settled', {
                'conversation_id': f'{self.inputs.namespace}_maint', 'user_id': self.inputs.user,
                'actor': 'human', 'status': 'completed',
                'projection': {
                    'content': (
                        f'maintenance round {i} '
                        + 'lorem ipsum dolor sit amet ' * 8
                    ),
                },
                'lane_id': 'main', 'command_id': f'{self.inputs.namespace}_maint_{i}_command',
                'turn_id': f'{self.inputs.namespace}_maint_turn_{i:06d}',
                'created_at': at, 'now': at,
                'conversation_defaults': {
                    'allowCreate': False,
                    'title': 'Maintenance reopen target',
                    'createdAt': self.inputs.conversation_timestamp, 'settings': {},
                },
            }, command=True)

    def maintenance_reopen_runner(self, engine, setup, operation, iterations):
        """Graceful stop + reopen cycles on the seeded authority.

        The charter's lifecycle gate: reopen wall is the headline statistic
        (flat summary keys below), shutdown captures the clean catch-up
        (CONTROL publication for tofu-db, WAL truncation on SQLite close),
        and first_read prices the first query after recovery. A failed cycle
        is loud, never a dropped sample.
        """
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        if setup is not None:
            setup(engine)
        restart = getattr(engine, 'restart_cycle', None)
        if restart is None:
            return {'error': f'{engine.name} does not expose restart cycles'}
        # Cold-read families mirror the app boot warm-up set. The 2026-09-16/18
        # timeout storm showed every cold family pays the full first-read cost
        # after a restart while each later read is nearly free; track both per
        # family so cold-path optimizations are visible in this report.
        cold_families: list[tuple[str, dict, bool]] = [
            ('record.get', {'namespace': self.inputs.namespace, 'key': self.inputs.record_key(0)}, False),
            ('record.list', {'namespace': self.inputs.namespace, 'prefix': 'k/', 'limit': 5}, False),
            ('conversation.count', {'user_id': self.inputs.user}, False),
            ('conversation.list', {'user_id': self.inputs.user, 'limit': 1}, False),
            ('task_results.recovery_pending', {'limit': 1}, False),
            ('timer.active.count', {'user_id': self.inputs.user}, False),
            ('credential.list', {'owner_user_id': self.inputs.user, 'tenant_id': ''}, False),
        ]
        shutdowns: list[float] = []
        reopens: list[float] = []
        first_reads: list[float] = []
        cold_reads: dict[str, list[float]] = {family: [] for family, _, _ in cold_families}
        warm_reads: dict[str, list[float]] = {family: [] for family, _, _ in cold_families}
        family_errors: dict[str, str] = {}
        expected_record = json.dumps(self.ports.record_value(0), sort_keys=True, allow_nan=False)

        def verify_record(value):
            # The raw floor intentionally omits the semantic record envelope;
            # production native/SQL adapters return value/version/update time.
            if not isinstance(engine, self.ports.raw_sqlite_type):
                if not isinstance(value, dict) or 'value' not in value:
                    raise self.ports.error('maintenance record.get returned missing or changed data')
                value = value['value']
            if json.dumps(value, sort_keys=True, allow_nan=False) != expected_record:
                raise self.ports.error('maintenance record.get returned missing or changed data')

        for _ in range(iterations):
            shutdown_ms, reopen_ms = restart()
            for family, payload, write in cold_families:
                if family in family_errors:
                    continue
                began = time.perf_counter_ns()
                try:
                    value = self.ports.execute(engine, family, payload, write)
                except self.ports.unsupported_error as exc:
                    # Only a declared capability gap before any sample may be
                    # omitted. I/O/timeout/recovery failures invalidate the
                    # phase, and every round needs its own required first read.
                    if family == 'record.get' or cold_reads[family]:
                        raise
                    family_errors[family] = str(exc)[:120]
                    continue
                elapsed_ms = (time.perf_counter_ns() - began) / 1e6
                if family == 'record.get':
                    verify_record(value)
                cold_reads[family].append(elapsed_ms)
                began = time.perf_counter_ns()
                value = self.ports.execute(engine, family, payload, write)
                elapsed_ms = (time.perf_counter_ns() - began) / 1e6
                if family == 'record.get':
                    verify_record(value)
                warm_reads[family].append(elapsed_ms)
            first_reads.append(cold_reads['record.get'][-1])
            shutdowns.append(shutdown_ms)
            reopens.append(reopen_ms)
        result = self.ports.summarize(reopens)
        result.update({
            'scope': 'maintenance',
            'rounds': iterations,
            'shutdown': self.ports.summarize(shutdowns),
            'first_read': self.ports.summarize(first_reads),
            'cold_read': {
                family: self.ports.summarize(samples)
                for family, samples in cold_reads.items() if samples
            },
            'warm_read': {
                family: self.ports.summarize(samples)
                for family, samples in warm_reads.items() if samples
            },
        })
        if family_errors:
            result['cold_read_unsupported'] = family_errors
        return result

    def crash_setup(self, engine):
        engine.execute('conversation.create', {
            'conv_id': f'{self.inputs.namespace}_crash_target', 'user_id': self.inputs.user,
            'title': 'Crash recovery target', 'settings': {},
            'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
        }, command=True)

    def crash_op(self, i):
        at = self.inputs.conversation_timestamp + 9_500_000 + i
        return ('turn.append_settled', {
            'conversation_id': f'{self.inputs.namespace}_crash_target', 'user_id': self.inputs.user,
            'actor': 'human', 'status': 'completed',
            'projection': {'content': f'crash round {i}'},
            'lane_id': 'main', 'command_id': f'{self.inputs.namespace}_crash_{i}_command',
            'turn_id': f'{self.inputs.namespace}_crash_turn_{i:06d}', 'created_at': at,
            'now': at,
            'conversation_defaults': {
                'allowCreate': False, 'title': 'Crash recovery target',
                'createdAt': self.inputs.conversation_timestamp, 'settings': {},
            },
        }, True)

    def crash_msg_count(self, engine) -> int:
        document = engine.execute('conversation.get', {
            'conv_id': f'{self.inputs.namespace}_crash_target', 'user_id': self.inputs.user,
            'message_window': 1,
        }, False)
        metadata = (document or {}).get('metadata') or {}
        count = metadata.get('msg_count')
        if not isinstance(count, int):
            raise self.ports.error('crash phase could not read a turn count')
        return count

    def crash_recovery_runner(self, engine, setup, operation, iterations):
        """SIGKILL mid-load: recovery wall, first read, zero acknowledged loss.

        Crash-recovery latency is a first-class engine metric. Each round
        appends ``crash_appends`` acknowledged turns, SIGKILLs the storage
        process with no graceful catch-up, then measures the
        kill-to-serving wall, the first windowed read after recovery, exact
        acknowledged-turn visibility (zero loss), and writability after
        recovery. A wrong count fails the phase loudly.
        """
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        if setup is not None:
            setup(engine)
        crash = getattr(engine, 'crash_cycle', None)
        if crash is None:
            return {'error': f'{engine.name} does not expose crash cycles'}
        appends = self.inputs.scale['crash_appends']
        recovers: list[float] = []
        first_reads: list[float] = []
        expected = 0
        appended = 0
        for _ in range(iterations):
            for _ in range(appends):
                op, payload, command = operation(appended)
                self.ports.execute(engine, op, payload, command)
                appended += 1
            expected += appends
            if self.crash_msg_count(engine) != expected:
                raise self.ports.error(
                    f'pre-crash turn count drifted from {expected} acknowledged')
            recovers.append(crash())
            began = time.perf_counter_ns()
            visible = self.crash_msg_count(engine)
            first_reads.append((time.perf_counter_ns() - began) / 1e6)
            if visible != expected:
                raise self.ports.error(
                    f'crash recovery lost acknowledged turns: {visible} of '
                    f'{expected} visible after reopen')
            op, payload, command = operation(appended)
            self.ports.execute(engine, op, payload, command)
            appended += 1
            expected += 1
            if self.crash_msg_count(engine) != expected:
                raise self.ports.error('authority is not writable after crash recovery')
        result = self.ports.summarize(recovers)
        embedded = getattr(engine, '_client_mode', None) == 'embedded'
        method = ('connection_close_reopen' if engine.name == 'rawsqlite' else
                  'embedded_abandon_reopen' if embedded else 'process_sigkill')
        result.update({
            'scope': ('graceful_reopen_control' if engine.name == 'rawsqlite' else
                      'embedded_abandon_control' if embedded else 'crash_recovery'),
            'recovery_method': method,
            'certifies_process_crash': method == 'process_sigkill',
            'rounds': iterations,
            'crash_appends': appends,
            'acknowledged_turns_recovered': expected,
            'zero_acknowledged_loss': True,
            'first_read': self.ports.summarize(first_reads),
        })
        return result
