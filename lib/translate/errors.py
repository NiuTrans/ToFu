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


__all__ = ['TranslationContentRefused']
