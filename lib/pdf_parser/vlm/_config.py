"""lib/pdf_parser/vlm/_config.py — VLM parse configuration & model discovery.

Holds the env-int helper, the VLM system prompt, and the model-discovery
helper used by the synchronous parse path.

Speed knobs (env-tunable, all optional):
    PDF_VLM_BATCH_PAGES   — pages per VLM call (default 4). Larger = fewer
                            HTTP round trips, fewer 429-cycles, but more
                            tokens per call (and a higher chance the model
                            hits its output cap on dense pages). Set to 1
                            to restore the legacy one-page-per-call mode.
    PDF_VLM_MAX_WORKERS   — concurrent VLM calls (default = number of
                            batches, i.e. fully parallel). Lower this on
                            shared keys to avoid 429 storms.
    PDF_VLM_MAX_TOKENS    — output-token cap per call (default 16384,
                            scaled with batch size).
"""

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _env_int(name: str, default: int, lo: int = 1, hi: int = 1024) -> int:
    """Parse an env var as an int, with bounds. Logs at debug on bad input."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        v = int(raw)
    except (ValueError, TypeError) as e:
        logger.debug('[PDF] %s=%r ignored, using default %d (%s)',
                     name, raw, default, e)
        return default
    return max(lo, min(hi, v))


_VLM_SYSTEM_PROMPT = """\
You are a precise document transcriber. Convert the provided PDF page image(s) into clean Markdown.

Rules:
- Preserve ALL text content faithfully — do not summarize or omit anything.
- Tables → Markdown pipe tables with header separators (| col | col |\\n|---|---|).
- Mathematical formulas:
  • Inline formulas → LaTeX in single dollars: $E = mc^2$
  • Display / block formulas → LaTeX in double dollars on their own lines:
    $$
    \\int_0^\\infty e^{-x^2} dx = \\frac{\\sqrt{\\pi}}{2}
    $$
  • Multi-line aligned equations → use \\\\begin{aligned}...\\\\end{aligned} inside $$.
- Tables MUST be transcribed as Markdown pipe tables — NEVER summarize a table into a
  single line or bracket notation.  Even if a table has colors, symbols (✓✗), or unusual
  formatting, reproduce every row and column faithfully as a pipe table.
- Figures / charts (NOT tables) → [Figure N: brief description of what it shows].
  A figure is an image, graph, or diagram — NOT a data table.
- Section headings → proper Markdown heading levels (# ## ###).
- Bullet / numbered lists → preserve as-is.
- Do NOT add commentary, explanation, or meta-text — output ONLY the transcribed Markdown.
- When content continues across page boundaries, just continue naturally without page markers.\
"""


def _get_vlm_models() -> list[str]:
    """Return list of available VLM-capable models for parallel dispatch."""
    try:
        from lib.llm_dispatch import get_dispatcher
        d = get_dispatcher()
        seen = []
        for s in d.pick_best_slots('vision', n=10):
            if s.model not in seen:
                seen.append(s.model)
        if seen:
            return seen
    except Exception as e:
        logger.warning('[PDF] VLM model discovery via dispatcher failed, using fallback: %s',
                       e, exc_info=True)
    from lib import GEMINI_MODEL
    return [GEMINI_MODEL or 'gemini-2.5-flash']
