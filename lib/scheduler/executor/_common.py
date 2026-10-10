"""lib/scheduler/executor/_common.py — Shared helpers for scheduler tool execution."""

from lib.log import get_logger

logger = get_logger(__name__)


def _coerce_int_arg(name, raw, default):
    """Coerce an LLM-supplied tool arg to int, with warning on fallback.

    LLM tool-call arguments sometimes arrive as strings (e.g. ``"60"``)
    even for parameters declared as integers in the tool schema. Rather
    than raising ``TypeError`` deep inside the call chain, we coerce
    here and fall back to *default* with a warning if the value can't
    be parsed.
    """
    if isinstance(raw, bool):
        # bool is a subclass of int — reject explicitly to avoid
        # silently mapping True→1 / False→0 for numeric params.
        logger.warning('[Timer] Boolean %s=%r passed as numeric — '
                       'coerced to default %d', name, raw, default)
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as _e:
        logger.warning('[Timer] Non-integer %s=%r — coerced to default %d '
                       '(reason: %s)', name, raw, default, _e)
        return default
