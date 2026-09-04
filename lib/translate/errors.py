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


class TranslationProviderQueueFull(RuntimeError):
    """The shared provider gate cannot retain another waiting caller."""

    retryable = True

    def __init__(self, *, capacity: int):
        self.capacity = int(capacity)
        super().__init__(
            'Translation provider queue is full; retry shortly '
            f'(capacity={self.capacity})'
        )


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
    'TranslationNoAdmissibleProvider',
    'TranslationProviderQueueFull',
]
