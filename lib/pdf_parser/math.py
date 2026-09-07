"""lib/pdf_parser/math.py — Math formula detection and wrapping.

Detects standalone formula lines and multi-line formula blocks in extracted
PDF text and wraps them with $$ LaTeX fences.
"""

import re

__all__ = ['postprocess_math_blocks']

# ═══════════════════════════════════════════════════════
#  Constants / regex
# ═══════════════════════════════════════════════════════

MATH_SYMBOLS = set(
    '∑∏∫∂∇∆∀∃∈∉⊂⊃⊆⊇∪∩≤≥≠≈≡±∓×÷√∞∝∠°'
    'αβγδεζηθικλμνξπρσςτυφχψω'
    'ΓΔΘΛΞΠΣΦΨΩ'
)
MATH_FUNCS = re.compile(
    r'\b(log|ln|exp|sin|cos|tan|lim|sup|inf|max|min|'
    r'arg\s?max|arg\s?min|softmax|sigmoid|ReLU|tanh|det|tr|diag|Pr|E)\b', re.I)
MATH_OPS = re.compile(r'[=<>≤≥≠≈]{1,2}')
EQ_PATTERN = re.compile(
    r'^[A-Za-z_\\][A-Za-z_0-9\\(){}αβγδεθλμσφψω,\s]*\s*[=:].*[+\-*/∑∏∫()\[\]]')
INTRO_LINE = re.compile(
    r'(defined as|given by|computed as|expressed as|written as|'
    r'formulated as|equal to|where)\s*[:.]?\s*$', re.I)
EQ_NUM = re.compile(r'\([\dA-Z]+\.?\d*\)\s*$')


def _is_math_line(stripped: str, prev_line: str = '') -> bool:
    """Heuristic check whether a stripped line looks like a math formula."""
    if not stripped or len(stripped) <= 3:
        return False
    if stripped.startswith('|') or stripped.startswith('#'):
        return False
    if stripped.startswith('$$'):
        return False

    has_symbols = any(c in MATH_SYMBOLS for c in stripped)
    has_funcs = bool(MATH_FUNCS.search(stripped))
    has_ops = bool(MATH_OPS.search(stripped))
    has_eq_pattern = bool(EQ_PATTERN.match(stripped))
    has_eq_num = bool(EQ_NUM.search(stripped))

    dot_count = stripped.count('·')
    is_degraded = dot_count >= 2 and has_ops
    has_func_eq = bool(re.match(r'^[A-Za-z_][A-Za-z_0-9·]*\(.*\)\s*=', stripped))

    if has_symbols and has_ops:
        return True
    if is_degraded:
        return True
    if has_func_eq and has_funcs:
        return True
    if has_eq_pattern and (has_funcs or has_symbols):
        return True
    if prev_line and INTRO_LINE.search(prev_line) and has_ops and (
            has_funcs or has_symbols or has_eq_pattern or has_func_eq):
        return True
    if has_eq_num and has_ops and len(stripped) > 10:
        return True
    return False


def _is_formula_continuation(stripped: str) -> bool:
    """Check if a line looks like a continuation of a multi-line formula."""
    if not stripped or len(stripped) <= 1:
        return False
    if stripped.startswith('|') or stripped.startswith('#'):
        return False
    if stripped.startswith('$$'):
        return False

    if stripped[0] in '+-=×·&≤≥≠≈∈⊂⊃∀∃\\':
        return True
    if re.match(r'^(where|s\.?t\.?|subject\s+to|for\s+all|such\s+that)\b', stripped, re.I):
        return True
    math_chars = sum(1 for c in stripped if c in MATH_SYMBOLS or c in '=+-*/()[]{}^_')
    if len(stripped) > 3 and math_chars / len(stripped) > 0.20:
        return True
    if EQ_NUM.match(stripped) and len(stripped) < 15:
        return True
    return False


def postprocess_math_blocks(md_text: str) -> str:
    """Wrap standalone formula lines AND multi-line formula blocks with $$ fences."""
    lines = md_text.split('\n')
    result = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()
        prev_stripped = lines[i - 1].strip() if i > 0 else ''

        if _is_math_line(stripped, prev_stripped):
            block_lines = [stripped]
            eq_label = ''
            m = EQ_NUM.search(stripped)
            if m:
                eq_label = f'  {m.group()}'
                block_lines[0] = EQ_NUM.sub('', stripped).strip()

            j = i + 1
            while j < n:
                next_stripped = lines[j].strip()
                if not next_stripped:
                    if (j + 1 < n and
                            (_is_math_line(lines[j + 1].strip(), '') or
                             _is_formula_continuation(lines[j + 1].strip()))):
                        block_lines.append('')
                        j += 1
                        continue
                    break
                if _is_formula_continuation(next_stripped) or _is_math_line(next_stripped, block_lines[-1]):
                    m2 = EQ_NUM.search(next_stripped)
                    if m2 and not eq_label:
                        eq_label = f'  {m2.group()}'
                        next_stripped = EQ_NUM.sub('', next_stripped).strip()
                    block_lines.append(next_stripped)
                    j += 1
                else:
                    break

            formula = '\n'.join(block_lines)
            result.append('')
            result.append(f'$$ {formula} $${eq_label}')
            result.append('')
            i = j
        else:
            result.append(line)
            i += 1

    return '\n'.join(result)
