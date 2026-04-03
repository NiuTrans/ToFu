#!/usr/bin/env python3
r"""
fix_openreview_latex.py  v3 — Auto-fix LaTeX-in-Markdown for OpenReview / GitHub / any CommonMark+MathJax/KaTeX.

CommonMark treats  \<ASCII-punctuation>  as an escape sequence and eats the backslash.
ASCII punctuation = ! " # $ % & ' ( ) * + , - . / : ; < = > ? @ [ \ ] ^ _ ` { | } ~

Additionally:
  - Bare `*` in math triggers Markdown emphasis/bold
  - Bare `<letter` in math triggers HTML tag parsing
  - Some renderers don't support \thinspace, \thickspace etc.

This script rewrites dangerous LaTeX patterns to safe equivalents — ALL inside math delimiters only.

Usage:
    python fix_openreview_latex.py input.md -o output.md   # fix file
    python fix_openreview_latex.py --check input.md         # dry-run check
    python fix_openreview_latex.py --raw                    # fix raw LaTeX from stdin
    python fix_openreview_latex.py --rules                  # show all rules
    python fix_openreview_latex.py --test                   # run self-tests
"""

import re, sys, argparse

# =====================================================
# Rule definitions — (compiled_regex, replacement, description)
# ORDER MATTERS: compound patterns before their sub-patterns!
# =====================================================

RULES = []

def rule(pattern, repl, desc):
    RULES.append((re.compile(pattern), repl, desc))

# ── 0: COMPOUND patterns first ──

# \perp\!\!\!\perp  →  \perp\mkern-9mu\perp
rule(r'\\perp\s*(?:\\[!]\s*){2,4}\\perp',
     r'\\perp\\mkern-9mu\\perp',
     r'\perp\!\!\!\perp -> \perp\mkern-9mu\perp')

# ── 1-4: Spacing commands → \mkern ──
rule(r'\\,',           r'\\mkern3mu ',    r'\, -> \mkern3mu')
rule(r'\\;',           r'\\mkern5mu ',    r'\; -> \mkern5mu')
rule(r'\\:',           r'\\mkern4mu ',    r'\: -> \mkern4mu')
rule(r'\\!',           r'\\mkern-3mu ',   r'\! -> \mkern-3mu')

# ── 5-10: Long-form spacing → \mkern ──
rule(r'\\thinspace\b',    r'\\mkern3mu ',   r'\thinspace  -> \mkern3mu')
rule(r'\\thickspace\b',   r'\\mkern5mu ',   r'\thickspace -> \mkern5mu')
rule(r'\\medspace\b',     r'\\mkern4mu ',   r'\medspace   -> \mkern4mu')
rule(r'\\negthinspace\b', r'\\mkern-3mu ',  r'\negthinspace  -> \mkern-3mu')
rule(r'\\negmedspace\b',  r'\\mkern-4mu ',  r'\negmedspace   -> \mkern-4mu')
rule(r'\\negthickspace\b',r'\\mkern-5mu ',  r'\negthickspace -> \mkern-5mu')

# ── 11-13: Delimiters  \{  \}  \|  ──
rule(r'\\\{',          r'\\lbrace ',     r'\{ -> \lbrace')
rule(r'\\\}',          r'\\rbrace ',     r'\} -> \rbrace')
rule(r'\\\|',          r'\\Vert ',       r'\| -> \Vert')

# ── 14-15: Bare < and > in math → \lt \gt ──
# Inside math blocks there are NO HTML tags, so we replace ALL bare < and >
# Only skip \lt \gt \le \leq \left< etc. (preceded by backslash)
rule(r'(?<!\\)(?<!\\left)(?<!\\right)<',  r'\\lt ',   r'bare < -> \lt')
rule(r'(?<!\\)(?<!\\left)(?<!\\right)>',  r'\\gt ',   r'bare > -> \gt')

# ── 16: Bare * in math → \ast ──
# * triggers Markdown emphasis. Replace with \ast (safe in all renderers).
# Skip \* (already escaped - though rare in math)
rule(r'(?<!\\)\*',     r'\\ast ',        r'bare * -> \\ast (prevent emphasis)')

# ── 17: \underbrace{...}_{label} → \underset{label}{\underbrace{...}} ──
def fix_underbrace(m):
    body = m.group(1)
    label = m.group(2)
    return rf'\underset{{{label}}}{{\underbrace{{{body}}}}}'

RULES.append((
    re.compile(r'\\underbrace\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}_\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}'),
    fix_underbrace,
    r'\underbrace{A}_{B} -> \underset{B}{\underbrace{A}}'
))

# ── 18: }_ trigger → }\mkern0mu_ ──
rule(r'\}(?=_)',       r'}\\mkern0mu',   r'}_ italic trigger -> }\mkern0mu_')

# ── 19-21: \rbrace _, \Vert _, \lbrace _ → remove space before subscript ──
# After rules 11-13 convert \} → \rbrace, \| → \Vert, \{ → \lbrace,
# the original }_ becomes \rbrace _ which still triggers emphasis in marked.
# Fix: \rbrace _ → \rbrace\mkern0mu_ (ditto for \Vert, \lbrace)
rule(r'\\rbrace\s+(?=_)',  r'\\rbrace\\mkern0mu', r'\\rbrace _ -> \\rbrace\\mkern0mu_ (prevent emphasis)')
rule(r'\\Vert\s+(?=_)',    r'\\Vert\\mkern0mu',   r'\\Vert _ -> \\Vert\\mkern0mu_ (prevent emphasis)')
rule(r'\\lbrace\s+(?=_)',  r'\\lbrace\\mkern0mu', r'\\lbrace _ -> \\lbrace\\mkern0mu_ (prevent emphasis)')

# ── 22: |X| absolute value → \lvert X \rvert ──
rule(r'\|\\(mathcal|mathbb|mathbf|mathfrak)\s+([A-Za-z])\|',
     r'\\lvert\\' + r'\1 \2\\rvert',
     r'|\mathcal V| -> \lvert\mathcal V\rvert')


# =====================================================
# Math-block extraction and per-block fixing
# =====================================================

MATH_PATTERN = re.compile(
    r'(\$\$[\s\S]*?\$\$)'             # display math $$...$$
    r'|'
    r'(\$(?!\$)(?:[^$\\]|\\.)+?\$)'   # inline math $...$
    r'|'
    r'(\\\([\s\S]*?\\\))'             # inline \(...\)
    r'|'
    r'(\\\[[\s\S]*?\\\])',            # display \[...\]
    re.DOTALL
)

def fix_math_block(latex: str) -> str:
    """Apply all rules to a single math block."""
    for pat, repl, _desc in RULES:
        if callable(repl):
            latex = pat.sub(repl, latex)
        else:
            latex = pat.sub(repl, latex)
    return latex

def fix_markdown(text: str) -> str:
    """Fix all math blocks in a Markdown document."""
    def replacer(m):
        return fix_math_block(m.group(0))
    
    result = MATH_PATTERN.sub(replacer, text)
    
    # Rule: ensure $$ is on its own line
    result = re.sub(r'([^\n])(\$\$)\s*\n', r'\1\n\2\n', result)
    result = re.sub(r'\n\s*(\$\$)([^\n$])', r'\n\1\n\2', result)
    
    return result


# =====================================================
# Check mode
# =====================================================

CHECK_PATTERNS = [
    (re.compile(r'\\[,;:!](?![a-zA-Z])'), 'Spacing cmd (\\, \\; \\: \\!) — backslash will be eaten'),
    (re.compile(r'\\[{}\|]'),              'Delimiter (\\{ \\} \\|) — backslash will be eaten'),
    (re.compile(r'\\underbrace\{[^}]*\}_\{'), '\\underbrace{A}_{B} — }_ triggers italic'),
    (re.compile(r'(?<!\\text)\}(?=_[^_])'), '}_ pattern — potential italic trigger'),
    (re.compile(r'\\perp\s*\\!\s*\\!'),    '\\perp\\!\\! — \\! will be eaten'),
    (re.compile(r'\\thinspace\b'),         '\\thinspace — may not be supported'),
    (re.compile(r'\\thickspace\b'),        '\\thickspace — may not be supported'),
    (re.compile(r'\\medspace\b'),          '\\medspace — may not be supported'),
    (re.compile(r'\\negthinspace\b'),      '\\negthinspace — may not be supported'),
    (re.compile(r'(?<!\\)\*'),             'Bare * — triggers Markdown emphasis'),
    (re.compile(r'(?<!\\)(?<!\\left)(?<!\\right)<'), 'Bare < — triggers HTML tag parsing'),
    (re.compile(r'(?<!\\)(?<!\\left)(?<!\\right)>'), 'Bare > — triggers HTML tag parsing'),
]

def check_markdown(text: str) -> list:
    issues = []
    for i, line in enumerate(text.split('\n'), 1):
        for m_block in MATH_PATTERN.finditer(line):
            block = m_block.group(0)
            for pat, desc in CHECK_PATTERNS:
                if pat.search(block):
                    issues.append((i, desc))
    return issues


# =====================================================
# Self-tests
# =====================================================

def run_tests():
    tests = [
        # Basic spacing
        (r'$a\,b$',           r'$a\mkern3mu b$',     'thin space'),
        (r'$a\;b$',           r'$a\mkern5mu b$',     'thick space'),
        (r'$a\:b$',           r'$a\mkern4mu b$',     'med space'),
        (r'$a\!b$',           r'$a\mkern-3mu b$',    'neg thin space'),
        
        # Long-form spacing -> mkern
        (r'$a\thinspace b$',     r'$a\mkern3mu  b$',     'thinspace -> mkern'),
        (r'$a\thickspace b$',    r'$a\mkern5mu  b$',     'thickspace -> mkern'),
        (r'$a\medspace b$',      r'$a\mkern4mu  b$',     'medspace -> mkern'),
        (r'$a\negthinspace b$',  r'$a\mkern-3mu  b$',    'negthinspace -> mkern'),
        
        # Delimiters
        (r'$\{x\}$',         r'$\lbrace x\rbrace $', 'braces'),
        (r'$\|x\|$',         r'$\Vert x\Vert $',     'double bar'),
        
        # Underbrace
        (r'$\underbrace{A+B}_{label}$',
         r'$\underset{label}{\underbrace{A+B}}$',     'underbrace'),
        
        # }_ trigger
        (r'$\mathbf{x}_n$',  r'$\mathbf{x}\mkern0mu_n$', '}_ trigger'),
        
        # \perp\!\!\!\perp (compound rule BEFORE \! rule)
        (r'$\perp\!\!\!\perp$',
         r'$\perp\mkern-9mu\perp$',                  'independence'),
        
        # |V| -> \lvert V \rvert
        (r'$|\mathcal V|$',
         r'$\lvert\mathcal V\rvert$',                 'abs value'),
        
        # No false positive on \leq, \lbrace etc.
        (r'$a \leq b$',      r'$a \leq b$',          'no false positive leq'),
        (r'$\lbrace x\rbrace$', r'$\lbrace x\rbrace$', 'no double fix'),
        
        # mkern passthrough
        (r'$a\mkern3mu b$',  r'$a\mkern3mu b$',      'mkern passthrough'),
        
        # No-brace mathbf
        (r'$\mathbf x_n$',   r'$\mathbf x_n$',       'no-brace mathbf ok'),
        
        # Real spacing
        (r'$\;\leq\;$',
         r'$\mkern5mu \leq\mkern5mu $',
         'real spacing'),

        # ── NEW: Bare * → \ast ──
        (r'$p_n^*$',         r'$p_n^\ast $',          'bare star -> ast'),
        (r'$w_n^{*}$',       r'$w_n^{\ast }$',       'star in braces -> ast'),
        (r'$H(p^*, q)$',     r'$H(p^\ast , q)$',     'star in expression'),
        
        # ── NEW: Bare < > in math (the actual bug!) ──
        (r'$i<n$',           r'$i\lt n$',             'bare < -> \\lt'),
        (r'$a > 0$',         r'$a \gt  0$',           'bare > -> \\gt'),
        (r'$\rbrace _{i<n}$', r'$\rbrace\mkern0mu_{i\lt n}$', 'rbrace < + _ trigger'),
        
        # ── NEW: \rbrace _, \Vert _, \lbrace _ emphasis prevention ──
        (r'$\rbrace _{sub}$',  r'$\rbrace\mkern0mu_{sub}$',  'rbrace space underscore'),
        (r'$\Vert _{sub}$',    r'$\Vert\mkern0mu_{sub}$',    'Vert space underscore'),
        (r'$\lbrace _{sub}$',  r'$\lbrace\mkern0mu_{sub}$',  'lbrace space underscore'),
        
        # Don't touch \leq, \left<, \right>, \lt, \gt
        (r'$a \leq b$',      r'$a \leq b$',          'leq untouched'),
        (r'$\left< x \right>$', r'$\left< x \right>$', 'left/right angle'),
        (r'$a \lt b$',       r'$a \lt b$',            'existing \\lt ok'),
        (r'$a \gt b$',       r'$a \gt b$',            'existing \\gt ok'),
    ]
    
    passed = failed = 0
    for inp, expected, name in tests:
        result = fix_markdown(inp)
        if result == expected:
            passed += 1
        else:
            failed += 1
            print(f'  FAIL [{name}]')
            print(f'    input:    {repr(inp)}')
            print(f'    expected: {repr(expected)}')
            print(f'    got:      {repr(result)}')
    
    print(f'\n  {passed} passed, {failed} failed')
    return failed == 0


# =====================================================
# CLI
# =====================================================

def main():
    parser = argparse.ArgumentParser(description='Fix LaTeX-in-Markdown for OpenReview')
    parser.add_argument('input', nargs='?', help='Input Markdown file')
    parser.add_argument('-o', '--output', help='Output file (default: stdout)')
    parser.add_argument('--check', action='store_true', help='Check for issues without fixing')
    parser.add_argument('--raw', action='store_true', help='Fix raw LaTeX from stdin (no $ needed)')
    parser.add_argument('--rules', action='store_true', help='Show all rules')
    parser.add_argument('--test', action='store_true', help='Run self-tests')
    
    args = parser.parse_args()
    
    if args.test:
        success = run_tests()
        sys.exit(0 if success else 1)
    
    if args.rules:
        print(f'Rules ({len(RULES)} total):')
        for i, (_, _, desc) in enumerate(RULES, 1):
            print(f'  {i:2d}. {desc}')
        return
    
    if args.raw:
        raw = sys.stdin.read().strip()
        wrapped = f'${raw}$'
        fixed = fix_markdown(wrapped)
        print(fixed[1:-1])
        return
    
    if not args.input:
        parser.print_help()
        return
    
    with open(args.input, 'r') as f:
        text = f.read()
    
    if args.check:
        issues = check_markdown(text)
        if issues:
            print(f'Found {len(issues)} potential issue(s):')
            for line_no, desc in issues:
                print(f'  ⚠  Line {line_no}: {desc}')
        else:
            print('No issues found. ✓')
        return
    
    fixed = fix_markdown(text)
    
    if args.output:
        with open(args.output, 'w') as f:
            f.write(fixed)
        print(f'Fixed → {args.output}')
    else:
        print(fixed)


if __name__ == '__main__':
    main()
