"""lib/fetch/pdf_extract.py — PDF text-processing utilities.

Contains manuscript line-number detection/removal and math formula
post-processing helpers.  The main extraction entry-point has moved
to lib.pdf_parser.extract_pdf_text().
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    '_strip_manuscript_line_numbers',
    '_postprocess_math_blocks',
]


# ═══════════════════════════════════════════════════════
#  Manuscript line-number stripping
# ═══════════════════════════════════════════════════════

def _strip_manuscript_line_numbers(text):
    """Remove line numbers commonly found in review/manuscript PDFs.

    Detects two patterns:
    1) Standalone number lines: lines that contain ONLY a number (1-5 digits),
       typically extracted by PyMuPDF as separate lines from the margin.
    2) Leading line numbers: e.g. "123  Some text here" at the start of lines,
       where the numbers increment roughly sequentially.

    A heuristic is used: if >30% of non-blank lines are standalone numbers,
    we consider the PDF a line-numbered manuscript and strip them.
    """
    lines = text.split('\n')
    non_blank = [l for l in lines if l.strip()]
    if len(non_blank) < 10:
        return text  # too short to judge

    # Pattern 1: standalone number lines (e.g. "  123  " on its own line)
    standalone_num = re.compile(r'^\s*\d{1,5}\s*$')
    num_count = sum(1 for l in non_blank if standalone_num.match(l))
    ratio = num_count / len(non_blank)

    if ratio > 0.15:
        # Many standalone number lines → strip them
        cleaned = [l for l in lines if not standalone_num.match(l)]
        removed = len(lines) - len(cleaned)
        if removed > 0:
            logger.debug('Stripped %d manuscript line-number lines '
                  '(%.0f%% of non-blank lines were standalone numbers)', removed, ratio * 100)
        return '\n'.join(cleaned)

    # Pattern 2: leading line numbers on text lines "  15  Some real text"
    # Check if many lines start with a small number followed by ≥2 spaces
    leading_num = re.compile(r'^(\d{1,5})([ \t]{2,})(.*)')
    matches = [leading_num.match(l) for l in non_blank]
    leading_count = sum(1 for m in matches if m and len(m.group(3).strip()) > 0)
    leading_ratio = leading_count / len(non_blank)

    if leading_ratio > 0.25:
        # Verify numbers are roughly sequential (at least 60% increments)
        nums = []
        for m in matches:
            if m and len(m.group(3).strip()) > 0:
                nums.append(int(m.group(1)))
        if len(nums) >= 5:
            increments = sum(1 for a, b in zip(nums, nums[1:]) if 0 < b - a <= 3)
            seq_ratio = increments / max(len(nums) - 1, 1)
            if seq_ratio > 0.4:
                def _strip_leading(line):
                    m = leading_num.match(line)
                    if m and len(m.group(3).strip()) > 0:
                        return m.group(3)
                    return line
                cleaned = [_strip_leading(l) for l in lines]
                logger.debug('Stripped leading line numbers from %d '
                      'lines (seq_ratio=%.0f%%)', leading_count, seq_ratio * 100)
                return '\n'.join(cleaned)

    return text


# ═══════════════════════════════════════════════════════
#  Math block post-processing
# ═══════════════════════════════════════════════════════

def _postprocess_math_blocks(md_text):
    """Wrap standalone formula lines with $$ fences for better LLM comprehension.

    Heuristics for identifying formula lines:
    - Contains math operators/symbols (Σ, ∫, ∏, ≤, ≥, ∈, ∀, ∃, →, ∞, etc.)
    - Contains patterns like f(x), log(...), lim, sup, inf, argmax, etc.
    - Contains fraction-like patterns: a/b surrounded by math context
    - Is preceded by a line like "is defined as:" or "is given by:" or "where"
    """
    MATH_SYMBOLS = set('∑∏∫∂∇∆∀∃∈∉⊂⊃⊆⊇∪∩≤≥≠≈≡±∓×÷√∞∝∠°αβγδεζηθικλμνξπρσςτυφχψωΓΔΘΛΞΠΣΦΨΩ')
    MATH_FUNCS = re.compile(r'\b(log|ln|exp|sin|cos|tan|lim|sup|inf|max|min|arg\s?max|arg\s?min|softmax|sigmoid|ReLU|tanh)\b', re.I)
    MATH_OPS = re.compile(r'[=<>≤≥≠≈]{1,2}')
    # Pattern: looks like an equation assignment  "L(θ) = ..." or "x_i = ..."
    EQ_PATTERN = re.compile(r'^[A-Za-z_\\][A-Za-z_0-9\\(){}αβγδεθλμσφψω,\s]*\s*[=:].*[+\-*/∑∏∫()\[\]]')
    INTRO_LINE = re.compile(r'(defined as|given by|computed as|expressed as|written as|formulated as|equal to|where)\s*[:.]?\s*$', re.I)
    # Equation number at end like "(1)" "(2.3)" "(A.1)"
    EQ_NUM = re.compile(r'\([\dA-Z]+\.?\d*\)\s*$')

    lines = md_text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip lines that are already in tables or headings
        if stripped.startswith('|') or stripped.startswith('#'):
            result.append(line)
            i += 1
            continue

        # Detect if this looks like a math formula line
        is_math = False
        if stripped and len(stripped) > 3:
            has_symbols = any(c in MATH_SYMBOLS for c in stripped)
            has_funcs = bool(MATH_FUNCS.search(stripped))
            has_ops = bool(MATH_OPS.search(stripped))
            has_eq_pattern = bool(EQ_PATTERN.match(stripped))
            has_eq_num = bool(EQ_NUM.search(stripped))

            # Degraded unicode detection: PyMuPDF replaces missing glyphs with ·
            dot_count = stripped.count('·')
            is_degraded = dot_count >= 2 and has_ops
            # func-like pattern: f(...) = ... with math ops/parens
            has_func_eq = bool(re.match(r'^[A-Za-z_][A-Za-z_0-9·]*\(.*\)\s*=', stripped))

            # Strong signal: math symbols + operators
            if has_symbols and has_ops:
                is_math = True
            # Degraded formula: multiple · + equation sign
            elif is_degraded:
                is_math = True
            # Function assignment with math functions: p(y|x) = exp(...) / ...
            elif has_func_eq and has_funcs:
                is_math = True
            # Equation pattern with function names
            elif has_eq_pattern and (has_funcs or has_symbols):
                is_math = True
            # Previous line is an intro line + current has operators
            elif i > 0 and INTRO_LINE.search(lines[i-1].strip()) and has_ops and (has_funcs or has_symbols or has_eq_pattern or has_func_eq):
                is_math = True
            # Has equation number at end like "(1)"
            elif has_eq_num and has_ops and len(stripped) > 10:
                is_math = True

        if is_math:
            # Strip trailing equation number for cleaner display
            formula = EQ_NUM.sub('', stripped).strip()
            eq_num_match = EQ_NUM.search(stripped)
            eq_label = f'  {eq_num_match.group()}' if eq_num_match else ''
            # Wrap in $$ block
            result.append('')
            result.append(f'$$ {formula} $${eq_label}')
            result.append('')
        else:
            result.append(line)
        i += 1

    return '\n'.join(result)

