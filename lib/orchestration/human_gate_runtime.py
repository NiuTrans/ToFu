"""Runtime request boundary for orchestration human gates.

The mutation-side :mod:`lib.orchestration.human_gate_service` resolves shared
approval/guidance registries. This module owns the symmetric execution side:
requesting a decision, emitting the stable gate lifecycle and adapting the
executor's abort callback to the task-like guidance interface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from lib.log import get_logger
from lib.orchestration._control_specs import (
    DEFAULT_HUMAN_APPROVAL_TIMEOUT,
)
from lib.orchestration.human_gate_events import (
    human_gate_notify_event,
    human_gate_request_event,
    human_gate_resolved_event,
)
from lib.orchestration.human_gate_request_identity import (
    HumanGateRequestIdentity,
)
from lib.orchestration.human_gate_runtime_ports import (
    ApprovalRequester,
    GuidanceRequester,
    HumanGateRequestPorts,
)
from lib.orchestration.human_gate_runtime_result import HumanGateRuntimeResult
from lib.orchestration._runtime_params import resolve_node_runtime_param


logger = get_logger(__name__)


class OrchestrationHumanGateRuntime:
    """Execute notify/approve/input gates behind explicit request ports."""

    def __init__(
        self,
        *,
        emit: Callable[[dict], None],
        abort_check: Callable[[], bool],
        ports: HumanGateRequestPorts | None = None,
        request_scope: str = '',
        identity: HumanGateRequestIdentity | None = None,
    ) -> None:
        self._emit = emit
        self._abort_check = abort_check
        self._ports = ports or HumanGateRequestPorts()
        self.identity = identity or HumanGateRequestIdentity(request_scope)

    def execute(self, node: dict, context: str) -> HumanGateRuntimeResult:
        mode = resolve_node_runtime_param(node, 'mode')
        prompt = str(
            resolve_node_runtime_param(node, 'prompt') or ''
        ).strip()
        node_id = node.get('id')
        label = node.get('name') or 'Human'

        if mode == 'notify':
            self._emit(human_gate_notify_event(
                node_id=node_id, name=label, prompt=prompt))
            logger.info('[HumanGateRuntime] notify node=%s', node_id)
            return HumanGateRuntimeResult(context=context)

        request_id = self.identity.next(node_id)
        self._emit(human_gate_request_event(
            node_id=node_id, name=label, mode=mode,
            prompt=prompt, request_id=request_id))
        logger.info('[HumanGateRuntime] request node=%s mode=%s id=%s',
                    node_id, mode, request_id)

        if mode == 'input':
            answer = self._ports.request_guidance(
                request_id,
                _AbortAwareTask(self._abort_check, request_id),
            )
            if answer is None:
                self._emit(human_gate_resolved_event(
                    node_id=node_id, mode=mode, request_id=request_id,
                    resolution='cancelled'))
                logger.info('[HumanGateRuntime] input %s aborted/cancelled',
                            request_id)
                return HumanGateRuntimeResult(context=context, aborted=True)
            answer = str(answer)
            self._emit(human_gate_resolved_event(
                node_id=node_id, mode=mode, request_id=request_id,
                resolution='answered', answer=answer))
            block = f'[Human input — {label}]\n{answer}'
            next_context = context + '\n\n' + block if context else block
            return HumanGateRuntimeResult(context=next_context)

        timeout = self._approval_timeout(resolve_node_runtime_param(
            node, 'timeout_sec'))
        approved = self._ports.request_approval(request_id, timeout)
        self._emit(human_gate_resolved_event(
            node_id=node_id, mode=mode, request_id=request_id,
            resolution='approved' if approved else 'rejected',
            approved=approved))
        if not approved:
            logger.info('[HumanGateRuntime] approval %s rejected/timed out',
                        request_id)
        return HumanGateRuntimeResult(context=context, aborted=not approved)

    @staticmethod
    def _approval_timeout(value: Any) -> int:
        try:
            return (
                int(value)
                if value not in (None, '')
                else DEFAULT_HUMAN_APPROVAL_TIMEOUT
            )
        except (ValueError, TypeError) as exc:
            logger.debug(
                '[HumanGateRuntime] invalid timeout; using %d: %s',
                DEFAULT_HUMAN_APPROVAL_TIMEOUT,
                exc,
            )
            return DEFAULT_HUMAN_APPROVAL_TIMEOUT


class _AbortAwareTask:
    """Adapt an abort callback to the mapping API used by guidance waits."""

    def __init__(self, abort_check: Callable[[], bool], request_id: str):
        self._abort_check = abort_check
        self._request_id = request_id

    def get(self, key, default=None):
        if key == 'aborted':
            try:
                return bool(self._abort_check())
            except Exception as exc:
                logger.debug('[HumanGateRuntime] abort check failed: %s', exc)
                return False
        if key == 'id':
            return self._request_id
        return default


__all__ = [
    'ApprovalRequester',
    'GuidanceRequester',
    'HumanGateRequestPorts',
    'HumanGateRequestIdentity',
    'HumanGateRuntimeResult',
    'OrchestrationHumanGateRuntime',
]
