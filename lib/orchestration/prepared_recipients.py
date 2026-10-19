"""Bind inspected occurrences to ordinary children and actual graph context owners.

Entry: prepare_flow_recipients. The caller owns accepted lineage, native group
readiness and graph execution. This scope prepares through agent_runner, binds
the journal's exact occurrence policy and joins ordinary child scopes. It never
walks a graph, settles a model/tool or reconstructs a completion receipt.
"""
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import threading
from types import MappingProxyType

from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig, OrchestrationInvocationRunner
from lib.orchestration.execution_journal import AgentExecutionAdmission, ExecutionJournal, RecoveryRefused, fingerprint
from lib.orchestration.recovery_frontier import PendingInvocationInput


class PreparedFlowRecipients:
    def __init__(self, inputs, entries):
        self._inputs, self._entries = inputs, entries
        self._used = set()
        self._closed = False
        self._journal = None
        self._lock = threading.Lock()

    @property
    def children(self):
        with self._lock:
            if self._closed:
                raise RecoveryRefused('Prepared Flow recipient scope is closed')
            return MappingProxyType({key: entry.child for key, entry in self._entries.items()})

    def source_bindings(self, pending_agents):
        """Match exact admitted sources to these children; grants no native rights."""
        with self._lock:
            if self._closed or set(pending_agents) != set(self._inputs):
                raise RecoveryRefused('Prepared Flow source inventory differs')
            bindings = []
            for key, expected in self._inputs.items():
                source = pending_agents[key]
                if (source.get('kind') != 'agent' or source.get('input_hash') != expected.input_hash
                        or not isinstance(source.get('invocation_id'), str) or not source['invocation_id']):
                    raise RecoveryRefused('Prepared Flow source admission differs')
                bindings.append((self._entries[key].child, source['invocation_id']))
            if len({source for _, source in bindings}) != len(bindings):
                raise RecoveryRefused('Prepared Flow source actors are repeated')
            return bindings

    def bind(self, journal):
        with self._lock:
            if (self._closed or self._journal is not None or not isinstance(journal, ExecutionJournal)
                    or set(self._inputs) & set(journal._root._records)):
                raise RecoveryRefused('Prepared Flow recipients require their pending root journal')
            preparations = {}
            for key, item in self._inputs.items():
                entry = self._entries[key]
                event = dict(type='execution_effect_prepared', key=key, kind='agent',
                    input_hash=item.input_hash, invocation_id=entry.child['id'])
                binding = entry.prepare_completion(event)
                if binding is not None:
                    preparations[key] = dict(event, invocation_completion=binding)
            journal.bind_agent_dispatch(self.dispatch, preparations=preparations)
            self._journal = journal

    def dispatch(self, *, admission, runner, node, context, iteration):
        with self._lock:
            if (self._closed or not isinstance(admission, AgentExecutionAdmission)
                    or admission.journal is not self._journal
                    or not isinstance(runner, OrchestrationInvocationRunner)):
                raise RecoveryRefused('Prepared Flow dispatch has no matching journal or runner')
            key = admission.key
            entry = self._entries.get(key)
            if entry is not None:
                expected = self._inputs[key]
                durable_node = {name: value for name, value in node.items() if name != '_runtimeAbortCheck'}
                if (key in self._used or admission.input_hash != expected.input_hash
                        or expected.node != durable_node or expected.context != context
                        or expected.iteration != iteration):
                    raise RecoveryRefused('Prepared Flow occurrence input changed or was repeated')
                self._used.add(key)
            elif self._used != set(self._inputs):
                raise RecoveryRefused('Flow reached a new agent before its complete prepared set')
        if entry is None:
            return runner(node, context, iteration, before_dispatch=admission)
        return entry.run(before_dispatch=admission, recipient_runner=runner,
                         runtime_abort_check=node.get('_runtimeAbortCheck'))

    def verify_consumed(self):
        with self._lock:
            if self._used != set(self._inputs):
                raise RecoveryRefused('Flow did not consume its complete prepared recipient set')

    def close(self):
        with self._lock:
            self._closed = True
            self._inputs.clear()
            self._entries.clear()
            self._used.clear()
            self._journal = None


@contextmanager
def prepare_flow_recipients(inputs, *, config, emit, abort_check):
    """Prepare every inspected child, retaining ordinary rejection/join ownership."""
    if not isinstance(config, OrchestrationAgentRunnerConfig) or not isinstance(inputs, dict) or not inputs:
        raise RecoveryRefused('Flow recipient preparation requires inspected inputs and runner policy')
    inputs = deepcopy(inputs)
    entries = {}
    with ExitStack() as stack:
        for key, item in inputs.items():
            if (not isinstance(item, PendingInvocationInput) or item.key != key
                    or fingerprint([item.node, item.context]) != item.input_hash):
                raise RecoveryRefused('Flow recipient input evidence changed')
            runner = OrchestrationInvocationRunner(config, emit=emit, abort_check=abort_check)
            if config.worker_context_policy == 'conversation':
                runner.restore_worker_context(item.worker_context)
            elif item.worker_context is not None:
                raise RecoveryRefused('Flow recipient context policy differs')
            stack.callback(runner.clear_worker_context)
            entries[key] = stack.enter_context(runner.prepare(item.node, item.context, item.iteration,
                durable_admission=True, transfer_context=True))
        recipients = PreparedFlowRecipients(inputs, entries)
        try:
            yield recipients
            recipients.verify_consumed()
        finally:
            recipients.close()
