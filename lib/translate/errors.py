"""Typed translation failures shared by engines and delivery adapters."""


class TranslationContentRefused(ValueError):
    """Every candidate failed a translation content-quality guard.

    ``ValueError`` compatibility is retained for existing engine callers while
    the structured fields let HTTP delivery project a typed refusal envelope.
    """

    def __init__(self, verdict, reason, *, attempts=0, content_fails=0):
        self.verdict = verdict
        self.reason = reason
        self.attempts = attempts
        self.content_fails = content_fails
        super().__init__(
            f'translation refused by content guard: verdict={verdict} '
            f'after {content_fails} content fails ({attempts} attempts) '
            f'— {reason}'
        )


class TranslationDeadlineExhausted(TimeoutError):
    """The caller's overall deadline ended translation without a result.

    ``dispatched_attempts`` counts attempts that entered dispatch. Zero means
    the budget was already spent when the chunk arrived: nothing was sent, so
    the outcome is "not dispatched", never an empty or failed model reply.
    """

    retryable = True

    def __init__(self, *, deadline_seconds, dispatched_attempts,
                 dispatch_fails=0, content_fails=0, elapsed_seconds=0.0,
                 last_err=None, chunk_label=''):
        self.deadline_seconds = float(deadline_seconds)
        self.dispatched_attempts = int(dispatched_attempts)
        self.dispatch_fails = int(dispatch_fails)
        self.content_fails = int(content_fails)
        self.elapsed_seconds = float(elapsed_seconds)
        self.last_err = last_err
        if self.dispatched_attempts:
            message = (
                f'Translation deadline ({self.deadline_seconds:.0f}s) expired '
                f'for chunk{chunk_label} after {self.dispatched_attempts} '
                f'dispatched attempt(s) ({self.dispatch_fails} dispatch '
                f'fails, {self.content_fails} content fails, '
                f'elapsed={self.elapsed_seconds:.0f}s, last_err={last_err})'
            )
        else:
            message = (
                f'Translation not dispatched for chunk{chunk_label}: its '
                f'{self.deadline_seconds:.0f}s overall deadline was exhausted '
                f'before dispatch'
            )
        super().__init__(message)


class TranslationNoAdmissibleProvider(RuntimeError):
    """No translation slot passed the request-local provider policy."""

    retryable = True

    def __init__(self):
        super().__init__(
            'No slot is currently admissible for optional translation; '
            'check Keys / Providers or wait for quota reset'
        )


__all__ = [
    'TranslationContentRefused',
    'TranslationDeadlineExhausted',
    'TranslationNoAdmissibleProvider',
]
