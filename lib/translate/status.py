"""Status-event formatting for the translation poll UI.

The engine fires status callbacks during the retry loop (rate limited,
truncated, etc.); we map those to terse English labels for the spinner.
The frontend receives both the formatted string AND the kind code, so it
can localize using i18n.
"""


def _format_status_message(event):
    """Translate a status-cb event dict into a short user-visible string.

    Kept deliberately terse — the frontend shows it next to the spinner.
    English text is emitted; the frontend i18n layer can optionally re-map
    ``kind`` codes for Chinese display (we also include the kind in the
    payload so the frontend can localize).
    """
    kind = event.get('kind', '')
    attempt = event.get('attempt', 0)
    elapsed = event.get('elapsed', 0) or 0
    # Map kinds to concise user-facing labels
    labels = {
        'started': 'Translating, please wait',
        'in_progress': 'Still translating',
        'rate_limited': 'All keys rate-limited, retrying',
        'dispatch_error': 'Provider error, retrying',
        'dispatch_failed_final': 'Provider errors exhausted',
        'empty_output': 'Empty response, retrying with another model',
        'empty_final': 'Empty response after retries',
        'truncated': 'Output truncated, retrying',
        'truncated_final': 'Output truncated after retries',
        'noop_output': 'Model echoed input, retrying with another model',
        'noop_final': 'Model echoed input after retries',
        'wrong_language': 'Output in wrong language, retrying with another model',
        'wrong_language_final': 'Output in wrong language after retries',
        'mt_fallback': 'MT provider failed, using LLM',
        'timed_out': 'Translation timed out, sending original text',
    }
    base = labels.get(kind, kind.replace('_', ' '))
    return f'{base} (attempt {attempt}, {int(elapsed)}s)'
