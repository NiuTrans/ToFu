"""Declared reader/writer populations, per-worker sessions and phase clocks.

Entry: ConcurrentWorkloads; dependencies are explicit inputs and execution ports.
"""
from __future__ import annotations

from dataclasses import dataclass
import time

from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts
from scripts.tofudb_workloads.records import RecordWorkloads
from scripts.tofudb_workloads.workers import WorkloadWorkers


@dataclass(frozen=True)
class ConcurrentWorkloads:
    inputs: WorkloadInputs
    ports: WorkloadPorts
    records: RecordWorkloads

    def concurrent_read_runner(self, engine, setup, operation, iterations):
        """One writer plus four concurrent readers (the harness's real shape).

        Reader latency is the headline statistic; writer throughput/p99 carry
        the charter's "reads must not block writer" gate. Any thread failure
        is loud: the engine's phase becomes an error, never a silently
        degraded sample set.
        """
        readers = 4
        writes = self.inputs.scale['concurrent_writes']
        reads_each = iterations
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        self.records.record_put_setup(engine)
        self.records.conv_seed_setup('cr')(engine)
        errors: list[str] = []
        workers = WorkloadWorkers(errors)
        reader_samples: list[list[float]] = [[] for _ in range(readers)]
        writer_samples: list[float] = []

        def reader(index: int) -> None:
            def run(session):
                for i in range(reads_each):
                    if workers.cancelled.is_set():
                        return
                    pick = (index + i) % 3
                    if pick == 0:
                        op, payload, command = self.records.record_get_op(i)
                    elif pick == 1:
                        op, payload, command = (
                            'conversation.get',
                            {'conv_id': self.inputs.conversation_id('cr', i % self.inputs.scale['seed_convs']),
                             'user_id': self.inputs.user},
                            False,
                        )
                    else:
                        op, payload, command = self.records.conv_list_op(i)
                    began = time.perf_counter_ns()
                    self.ports.execute(session, op, payload, command)
                    reader_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            workers.run_worker(engine, f'reader {index}', run)

        def writer() -> None:
            def run(session):
                for i in range(writes):
                    if workers.cancelled.is_set():
                        return
                    began = time.perf_counter_ns()
                    session.execute('record.put', {
                        'namespace': self.inputs.namespace, 'key': f'cw/{i:06d}',
                        'value': self.ports.record_value(i),
                    }, True)
                    writer_samples.append(
                        (time.perf_counter_ns() - began) / 1e6)
            workers.run_worker(engine, 'writer', run)

        for index in range(readers):
            workers.add(f'perf-reader-{index}', reader, index)
        workers.add('perf-writer', writer)
        phase_started = workers.run(120.0 + 0.1 * (writes + readers * reads_each))
        if errors:
            return {'error': '; '.join(errors)}
        if len(writer_samples) != writes:
            return {'error': (
                f'writer completed {len(writer_samples)}/{writes} ops: '
                'reads blocked the writer')}
        flat = [sample for per_reader in reader_samples
                for sample in per_reader]
        if not flat:
            return {'error': 'concurrent readers produced no samples'}
        if len(flat) != readers * reads_each:
            return {'error': f'concurrent readers completed {len(flat)}/{readers * reads_each} ops'}
        result = self.ports.summarize(flat)
        writer_stats = self.ports.summarize(writer_samples)
        phase_wall_seconds = (time.perf_counter_ns() - phase_started) / 1e9
        result.update({
            'scope': 'concurrent',
            'concurrent_readers': readers,
            'reads_completed': len(flat),
            'writer_iterations': len(writer_samples),
            # Wall-clock phase throughput, not 1/mean-latency: the latter
            # inflates concurrency whenever per-op latency varies.
            'writer_ops_per_sec': round(
                len(writer_samples) / phase_wall_seconds, 1)
                if phase_wall_seconds else 0.0,
            'writer_p50_ms': writer_stats['p50_ms'],
            'writer_p99_ms': writer_stats['p99_ms'],
        })
        return result

    def swarm_write_runner(self, engine, setup, operation, iterations):
        """W concurrent writers (one conversation each) + R concurrent readers.

        The real swarm shape: per-conversation sequential writers, concurrent
        across conversations. Aggregate writer throughput exposes the
        single-writer authority's serialization ceiling; per-writer latency
        keeps the interactive gate honest; reader latency proves reads stay
        unblocked. Any thread failure is loud, never a dropped sample.
        """
        writers = self.inputs.scale['swarm_writers']
        readers = self.inputs.scale['swarm_readers']
        writes_each = iterations
        reads_each = self.inputs.scale['swarm_reads_each']
        prepare = getattr(engine, 'prepare_workload', None)
        if prepare is not None:
            prepare()
        self.records.record_put_setup(engine)
        for writer in range(writers):
            engine.execute('conversation.create', {
                'conv_id': f'{self.inputs.namespace}_swarm_{writer}', 'user_id': self.inputs.user,
                'title': f'Swarm writer {writer}', 'settings': {},
                'created_at': self.inputs.conversation_timestamp, 'updated_at': self.inputs.conversation_timestamp,
            }, command=True)
        errors: list[str] = []
        workers = WorkloadWorkers(errors)
        writer_samples: list[list[float]] = [[] for _ in range(writers)]
        reader_samples: list[list[float]] = [[] for _ in range(readers)]

        def writer(index: int) -> None:
            conv = f'{self.inputs.namespace}_swarm_{index}'
            def run(session):
                for i in range(writes_each):
                    if workers.cancelled.is_set():
                        return
                    at = self.inputs.search_timestamp + 3_600_000 + i
                    began = time.perf_counter_ns()
                    session.execute('turn.append_settled', {
                        'conversation_id': conv, 'user_id': self.inputs.user,
                        'actor': 'human', 'status': 'completed',
                        'projection': {
                            'content': (
                                f'swarm agent {index} round {i} '
                                + 'lorem ipsum dolor sit amet ' * 8
                            ),
                        },
                        'lane_id': 'main',
                        'command_id': f'{conv}_{i}_command',
                        'turn_id': f'{conv}_turn_{i:06d}',
                        'created_at': at, 'now': at,
                        'conversation_defaults': {
                            'allowCreate': False,
                            'title': f'Swarm writer {index}',
                            'createdAt': self.inputs.conversation_timestamp, 'settings': {},
                        },
                    }, True)
                    writer_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            workers.run_worker(engine, f'writer {index}', run)

        def reader(index: int) -> None:
            def run(session):
                for i in range(reads_each):
                    if workers.cancelled.is_set():
                        return
                    pick = (index + i) % 3
                    if pick == 0:
                        op, payload, command = (
                            'conversation.get',
                            {'conv_id': f'{self.inputs.namespace}_swarm_{index % writers}',
                             'user_id': self.inputs.user},
                            False,
                        )
                    elif pick == 1:
                        op, payload, command = self.records.conv_list_op(i)
                    else:
                        op, payload, command = self.records.record_get_op(i)
                    began = time.perf_counter_ns()
                    self.ports.execute(session, op, payload, command)
                    reader_samples[index].append(
                        (time.perf_counter_ns() - began) / 1e6)
            workers.run_worker(engine, f'reader {index}', run)

        for index in range(writers):
            workers.add(f'perf-swarm-writer-{index}', writer, index)
        for index in range(readers):
            workers.add(f'perf-swarm-reader-{index}', reader, index)
        phase_started = workers.run(180.0 + 0.2 * (
            writers * writes_each + readers * reads_each))
        if errors:
            return {'error': '; '.join(errors)}
        flat_writers = [sample for per_writer in writer_samples
                        for sample in per_writer]
        flat_readers = [sample for per_reader in reader_samples
                        for sample in per_reader]
        if (len(flat_writers) != writers * writes_each or not flat_readers
                or len(flat_readers) != readers * reads_each):
            return {'error': (
                f'swarm phase incomplete: writers '
                f'{len(flat_writers)}/{writers * writes_each}, readers '
                f'{len(flat_readers)}/{readers * reads_each}')}
        result = self.ports.summarize(flat_writers)
        reader_stats = self.ports.summarize(flat_readers)
        phase_wall_seconds = (time.perf_counter_ns() - phase_started) / 1e9
        result.update({
            'scope': 'swarm',
            'swarm_writers': writers,
            'concurrent_readers': readers,
            'writer_iterations': len(flat_writers),
            # Headline: one writer's interactive append latency under swarm
            # pressure; wall-clock aggregate throughput exposes the
            # authority's serialization ceiling (never 1/mean-latency, which
            # inflates concurrency whenever per-op latency varies).
            'aggregate_writer_ops_per_sec': round(
                len(flat_writers) / phase_wall_seconds, 1)
                if phase_wall_seconds else 0.0,
            'writer_ops_per_sec': result['ops_per_sec'],
            'writer_p50_ms': result['p50_ms'],
            'writer_p99_ms': result['p99_ms'],
            'reader_p50_ms': reader_stats['p50_ms'],
            'reader_p99_ms': reader_stats['p99_ms'],
            'reads_completed': len(flat_readers),
        })
        return result
