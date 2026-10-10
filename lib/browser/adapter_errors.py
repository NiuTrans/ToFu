"""Typed failures shared by site adapters, the read pipeline, and site packs.

Kept in a leaf module so lib/browser/read_pipeline.py and lib/site_packs/
can raise the same errors invoke_adapter settles, without importing the
adapter registry itself.
"""


class AdapterValidationError(ValueError):
    pass


class AdapterExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable=False, details=None):
        self.code = code
        self.retryable = bool(retryable)
        self.details = dict(details or {})
        super().__init__(message)

    def as_dict(self) -> dict:
        return {'code': self.code, 'message': str(self),
                'retryable': self.retryable, 'details': self.details}
