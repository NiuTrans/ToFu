"""Transport-neutral terminal outcome model and classification policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from lib.agent_verdict import is_incomplete_stop
from lib.orchestration.wire_formats import OUTCOME_FORMAT


OUTCOME_CATEGORIES: Final = ('success', 'incomplete', 'failure', 'aborted')


@dataclass(frozen=True)
class TerminalOutcome:
    """Meaning of one terminal flow result across every application adapter."""

    category: str
    engine_status: str
    lifecycle_status: str
    chat_status: str
    ok: bool
    stop_reason: str
    finish_reason: str
    error: str = ''

    @property
    def runtime_error(self) -> str:
        if self.category == 'incomplete':
            return self.stop_reason
        if self.category == 'failure':
            return self.error or self.stop_reason or 'flow execution failed'
        return ''

    @property
    def error_envelope(self) -> dict | None:
        detail = self.runtime_error
        if not detail:
            return None
        from lib.error_envelope import make_envelope
        incomplete = self.category == 'incomplete'
        return make_envelope(
            'generic',
            message=(
                '编排流程未完成\nOrchestration flow did not complete'
                if incomplete else
                '编排流程执行失败\nOrchestration flow execution failed'
            ),
            detail=detail,
            context='orchestration:execution',
            source='lib.orchestration_outcome',
            raw=detail,
            severity='warning' if incomplete else 'error',
            retryable=False,
            hint=(
                '请查看运行轨迹和停止原因，调整输入或运行限制后重试。\n'
                'Review the trace and stop reason, then adjust the input or '
                'run limits before retrying.'
                if incomplete else
                '请查看运行轨迹和错误详情定位失败节点。\n'
                'Review the trace and error detail to locate the failed node.'
            ),
            extensions={'outcome': self.as_dict()},
        )

    @property
    def durable_error(self) -> dict | None:
        return self.error_envelope

    def as_dict(self) -> dict:
        return {
            'format': OUTCOME_FORMAT,
            'category': self.category,
            'engine_status': self.engine_status,
            'lifecycle_status': self.lifecycle_status,
            'chat_status': self.chat_status,
            'ok': self.ok,
            'stop_reason': self.stop_reason,
            'finish_reason': self.finish_reason,
            'error': self.error,
        }

    def event_fields(self) -> dict:
        return {
            'ok': self.ok,
            'stop_reason': self.stop_reason,
            'lifecycle_status': self.lifecycle_status,
            'outcome_category': self.category,
            'finish_reason': self.finish_reason,
            'outcome': self.as_dict(),
        }


def _outcome(
    category: str,
    engine_status: str,
    stop_reason: str,
    *,
    error: str = '',
) -> TerminalOutcome:
    if category == 'success':
        return TerminalOutcome(
            category, engine_status, 'done', 'done', True,
            stop_reason or 'completed', 'stop', '',
        )
    if category == 'incomplete':
        return TerminalOutcome(
            category, engine_status, 'error', 'done', False,
            stop_reason or 'incomplete', 'incomplete', '',
        )
    if category == 'aborted':
        return TerminalOutcome(
            category, 'aborted', 'aborted', 'aborted', False,
            'aborted', 'aborted', '',
        )
    return TerminalOutcome(
        'failure', engine_status or 'failed', 'error', 'error', False,
        stop_reason or 'failed', 'error', error,
    )


def classify_terminal_outcome(
    engine_status: str,
    *,
    error: str = '',
    failure_kind: str = '',
    reported_stop_reason: str = '',
    reported_ok: bool | None = None,
    loop_exits: list[dict] | tuple[dict, ...] = (),
    node_failures: list[dict] | tuple[dict, ...] = (),
) -> TerminalOutcome:
    """Classify an engine result once, before any transport projection."""
    status = str(engine_status or 'failed')
    detail = str(error or '')
    reason = str(reported_stop_reason or '')

    if status == 'aborted' or reason == 'aborted':
        return _outcome('aborted', status, 'aborted')
    if status == 'failed' or failure_kind:
        stable_reason = str(failure_kind or reason or 'failed')
        return _outcome('failure', 'failed', stable_reason, error=detail)

    failures = list(node_failures or ())
    if failures:
        first = failures[0] or {}
        return _outcome(
            'failure', status, 'node_failed',
            error=str(first.get('error') or detail or ''),
        )
    for exit_record in loop_exits or ():
        exit_reason = str((exit_record or {}).get('reason') or '')
        if is_incomplete_stop(exit_reason):
            return _outcome('incomplete', status, exit_reason)
    if is_incomplete_stop(reason):
        return _outcome('incomplete', status, reason)
    if reported_ok is False:
        return _outcome('failure', status, reason or 'failed', error=detail)
    return _outcome('success', status, reason or 'completed')


def outcome_from_result(
    result: dict | None,
    *,
    failure_kind: str = '',
) -> TerminalOutcome:
    """Normalize canonical or legacy engine facts through one classifier."""
    result = result if isinstance(result, dict) else {}
    embedded = result.get('outcome')
    embedded = embedded if isinstance(embedded, dict) else {}
    error = embedded.get('error')
    if error in (None, ''):
        error = result.get('error') or ''
    return classify_terminal_outcome(
        str(embedded.get('engine_status') or result.get('status') or 'failed'),
        error=str(error or ''),
        failure_kind=failure_kind,
        reported_stop_reason=str(
            embedded.get('stop_reason') or result.get('stop_reason') or ''),
        reported_ok=(
            embedded.get('ok')
            if isinstance(embedded.get('ok'), bool)
            else result.get('ok')
            if isinstance(result.get('ok'), bool)
            else None
        ),
        loop_exits=result.get('loop_exits') or (),
        node_failures=result.get('node_failures') or (),
    )


__all__ = [
    'OUTCOME_CATEGORIES',
    'OUTCOME_FORMAT',
    'TerminalOutcome',
    'classify_terminal_outcome',
    'outcome_from_result',
]
