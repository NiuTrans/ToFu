"""Durable run projection shared by worker execution and startup recovery."""

from __future__ import annotations

from dataclasses import dataclass

from lib.error_envelope import normalize_envelope
from lib.log import get_logger
from lib.orchestration.runtime_ports import OrchestrationDurableRunPort
from lib.orchestration.mutation_result import MUTATION_CONFLICT


logger = get_logger(__name__)


class DurableProjectionError(RuntimeError):
    """A durable run could not project an event or lifecycle fact."""


@dataclass(frozen=True)
class DurableFinalization:
    """Classification of one terminal durable-header transition."""

    accepted: bool = False
    abort_won: bool = False
    error: DurableProjectionError | None = None


class DurableRunProjection:
    """Fail-closed durable event and lifecycle projection for one run."""

    def __init__(self, runs: OrchestrationDurableRunPort, run_id: str):
        self._runs = runs
        self.run_id = str(run_id or '')

    def project_event(
        self, seq: int, event: dict, status: str = '',
    ) -> None:
        try:
            projected = self._runs.project_event(
                self.run_id, seq, event, status)
        except Exception as error:
            raise DurableProjectionError(
                'failed to atomically project durable orchestration event '
                f'{self.run_id}/{seq}') from error
        if projected is False:
            raise DurableProjectionError(
                'failed to atomically project durable orchestration event '
                f'{self.run_id}/{seq}')

    def finalize(self, status: str, *, final: str = '',
                 error: dict | str | None = None) -> DurableFinalization:
        """Commit one terminal fact and classify the accepted-abort race."""
        error = normalize_envelope(
            error,
            context='durable finalization',
            source='orchestration:durable-projection',
            require_complete=True,
        )
        try:
            transition = self._runs.transition_status(
                self.run_id,
                status,
                final=final,
                error=error,
            )
        except Exception as exception:
            logger.error(
                '[OrchestrationRuntime] durable finalization failed run=%s: %s',
                self.run_id, exception, exc_info=True)
            projection_error = DurableProjectionError(
                'failed to finalize durable orchestration run '
                f'{self.run_id}')
            projection_error.__cause__ = exception
            return DurableFinalization(error=projection_error)

        if transition.ok:
            return DurableFinalization(accepted=True)
        if (transition.reason == MUTATION_CONFLICT
                and transition.run_status == 'aborted'):
            logger.info(
                '[OrchestrationRuntime] persisted abort won terminal race '
                'run=%s', self.run_id)
            return DurableFinalization(abort_won=True)
        return DurableFinalization(error=DurableProjectionError(
            'failed to finalize durable orchestration run '
            f'{self.run_id} ({transition.reason})'))

    def record_error(self, error: dict | str | None, *,
                     context: str = 'projection failure') -> bool:
        """Best-effort terminal error used after worker/start projection loss."""
        envelope = normalize_envelope(
            error,
            context=context,
            source='orchestration:durable-projection',
            require_complete=True,
        )
        try:
            transition = self._runs.transition_status(
                self.run_id,
                'error',
                final='',
                error=envelope,
            )
        except Exception:
            logger.error(
                '[OrchestrationRuntime] failed to record %s run=%s',
                context, self.run_id, exc_info=True)
            return False
        if not transition.ok:
            logger.error(
                '[OrchestrationRuntime] %s status was not persisted '
                'run=%s reason=%s current=%s',
                context, self.run_id, transition.reason,
                transition.run_status)
            return False
        return True


__all__ = [
    'DurableProjectionError', 'DurableFinalization',
    'DurableRunProjection',
]
