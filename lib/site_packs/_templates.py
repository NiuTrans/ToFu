"""Shared parameter grammar for manifest references, URLs and form values.

URL templates quote values unless raw or floor is explicit. Form values retain
their existing verbatim interpolation. Admission sees every executable reference.
"""

import re
from urllib.parse import quote

_PARAMETER = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)(\|raw|\|floor:(\d+))?\}')


def template_params(template: str) -> set[str]:
    return {match.group(1) for match in _PARAMETER.finditer(template or '')}


def render_template(template: str, params: dict) -> str:
    """Render URL parameters, including explicitly requested floor transforms."""
    def replace(match: re.Match) -> str:
        name, modifier, floor = match.group(1), match.group(2), match.group(3)
        value = params.get(name)
        if floor is not None:
            try:
                return str(max(int(floor), int(value)))
            except (TypeError, ValueError):
                return str(int(floor))
        text = str(value or '')
        return text if modifier else quote(text)
    return _PARAMETER.sub(replace, template or '')


def render_value(template: str, params: dict) -> str:
    return _PARAMETER.sub(lambda match: str(params.get(match.group(1)) or ''), template or '')
