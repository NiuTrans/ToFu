"""Resource and API-attempt policy shared by translation workflows.

Entry point: :func:`translation_max_429_attempts`.
Dependencies: the launch-time canonical resource manifest only; provider
selection and retry mechanics remain owned by ``lib.llm_dispatch``.
"""

from runtime_guards import resolve_resource_budget


def translation_max_429_attempts() -> int:
    """Return the bounded upstream rate-limit attempts for one translation."""
    return resolve_resource_budget(
        'TOFU_TRANSLATE_MAX_429_ATTEMPTS',
        minimum=1,
        maximum=64,
    )
