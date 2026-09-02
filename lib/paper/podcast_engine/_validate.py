"""lib/paper/podcast_engine/_validate.py — the podcast script quality gates.

Zero-LLM deterministic validators that a generated script must pass BEFORE it
reaches the TTS stage (docs/modules/ingest_media.md). They exist because
the prompt's "MUST NOT" rules are advisory to an LLM; these functions are the
enforcement. Every gate returns a list of human-readable issue strings —
an empty list means pass. ``validate_script`` aggregates them all.

Gates:
  1a. LaTeX residue        — ``$...$``, ``\\frac``, ``^{ }`` …
  1b. Unicode math symbols — ``α β ² × ≤ ≈ →`` … (owner hole #1: these sail
      straight past a LaTeX-only regex and the TTS mangles them)
  1c. zh abbreviations     — watchlist tokens must appear in their required
      spoken form (owner hole #2: Chinese voices mangle ``LLM``/``KV cache``)
  2.  Number provenance    — every data number must trace to the source text,
      literally OR derived (difference / relative-change / ratio — owner
      hole #4: literal-only matching kills legitimate scripts like
      "up 3.2 percentage points" computed from 86.3 − 83.1)
  3.  Structure            — cold_open first (with a number), recap last,
      body depth, figure_ref whitelist
  4.  Duration             — estimated seconds within the mode's ±20% band
"""

from __future__ import annotations

import re

from lib.log import get_logger
from lib.paper.podcast_prompts import (
    MAX_FIGURE_SEGMENTS,
    PODCAST_MODES,
    SCRIPT_SECTIONS,
    ZH_ABBREV_WATCHLIST,
)

logger = get_logger(__name__)


# ── 1a. LaTeX residue ────────────────────────────────────────────────────

_LATEX_PATTERNS = (
    re.compile(r'\$\$[^$]+\$\$', re.DOTALL),     # display math $$...$$
    re.compile(r'\$[^$\n]+\$'),                  # inline math $...$
    re.compile(r'\\[a-zA-Z]+\s*\{'),             # \frac{ \sum{ \text{
    re.compile(r'\\[a-zA-Z]+\b'),                # \alpha \beta \times
    re.compile(r'\^\s*\{'),                      # ^{...}
    re.compile(r'_\s*\{'),                       # _{...}
)


def check_latex_residue(text: str) -> list[str]:
    """Return an issue per distinct LaTeX construct found in ``text``."""
    found: list[str] = []
    seen: set[str] = set()
    for pat in _LATEX_PATTERNS:
        for m in pat.finditer(text or ''):
            snippet = m.group(0)[:24]
            if snippet in seen:
                continue
            seen.add(snippet)
            found.append(f'残留 LaTeX 记号 "{snippet}"(口播稿禁止出现公式符号)')
            if len(found) >= 8:
                return found
    return found


# ── 1b. Unicode math symbols (owner hole #1) ─────────────────────────────

# Greek letters (upper+lower), super/subscripts, operators, arrows, set
# symbols. Deliberately EXCLUDES characters that double as normal prose:
# '·' (name separator), '°' (TTS reads it as 度 fine), '%' (universally
# handled), CJK punctuation.
MATH_SYMBOLS = frozenset(
    'αβγδεζηθικλμνξπρστυφχψω'
    'ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΠΡΣΤΥΦΧΨΩ'
    '⁰¹²³⁴⁵⁶⁷⁸⁹ⁿ'
    '₀₁₂₃₄₅₆₇₈₉'
    '×÷≤≥≈≠±√∑∏∫∂∇∞'
    '∈∉⊆⊂⊄∪∩∀∃¬∧∨∅'
    '→←↔⇒⇔↑↓⊥∝∘⊗⊕'
    '⟨⟩‖⌊⌋⌈⌉′″'
)


def check_unicode_math(text: str) -> list[str]:
    """Return an issue listing each raw math symbol found (must be verbalized)."""
    hits = sorted({ch for ch in (text or '') if ch in MATH_SYMBOLS})
    if not hits:
        return []
    return [
        '原始数学符号未口播化: ' + ' '.join(hits)
        + '(希腊字母写口播名如「阿尔法」,上标/运算符写成文字如「平方」「乘以」「不超过」)'
    ]


# ── 1c. zh abbreviation watchlist (owner hole #2) ────────────────────────


def check_abbreviations(text: str, lang: str) -> list[str]:
    """Flag watchlist tokens that appear RAW in a zh script.

    English scripts are exempt (English voices read acronyms fine). Matching
    is case-sensitive, longest-token-first, boundary-aware, and each matched
    span is masked so 'KV cache' does not also re-fire 'KV'.
    """
    if lang != 'zh' or not text:
        return []
    issues: list[str] = []
    masked = text
    for token in sorted(ZH_ABBREV_WATCHLIST, key=len, reverse=True):
        spoken = ZH_ABBREV_WATCHLIST[token]
        pat = re.compile(r'(?<![A-Za-z0-9])' + re.escape(token)
                         + r'(?![A-Za-z0-9])')
        hits = list(pat.finditer(masked))
        if hits:
            issues.append(f'缩写 "{token}" 未口播化(应作「{spoken}」)')
            masked = pat.sub(lambda m: ' ' * len(m.group(0)), masked)
    return issues


# ── 2. Number provenance, with derived-number support (owner hole #4) ────

_NUM_RE = re.compile(
    r'(\d+(?:,\d{3})*(?:\.\d+)?)\s*'
    r'(%|百分点|个\s*百分点|倍|percent|percentage\s*points?|pp\b)?')

_STRUCTURAL_YEAR_LO, _STRUCTURAL_YEAR_HI = 1900, 2099
_MAX_SOURCE_NUMBERS = 150  # bound the O(n²) derived-pair scan


def _decimals(raw: str) -> int:
    frac = raw.partition('.')[2]
    return len(frac)


def _parse_number(raw: str) -> float | None:
    try:
        return float(raw.replace(',', ''))
    except (ValueError, TypeError) as _e:
        logger.debug('parse number: unparseable/unexpected type (%s)', _e)
        return None


def extract_data_numbers(text: str) -> list[dict]:
    """Pull DATA-GRADE numbers out of prose (structural numerals exempt).

    A number counts as data when it has a decimal part, is ≥ 11, or carries a
    unit suffix (%, 百分点, 倍, percent…). Bare 0–10 integers ("三点收获") and
    bare years are structural — checking them would false-positive every
    outline sentence.
    """
    out: list[dict] = []
    for m in _NUM_RE.finditer(text or ''):
        raw, suffix = m.group(1), (m.group(2) or '')
        value = _parse_number(raw)
        if value is None:
            continue
        has_decimal = '.' in raw
        has_suffix = bool(suffix.strip())
        is_year = (_STRUCTURAL_YEAR_LO <= value <= _STRUCTURAL_YEAR_HI
                   and not has_decimal and not has_suffix)
        if is_year:
            continue
        if not has_decimal and not has_suffix and 0 <= value <= 10:
            continue  # structural small integer
        if not has_decimal and not has_suffix and value < 11:
            continue
        start = max(0, m.start() - 24)
        ctx = (text[start:m.end() + 12]).replace('\n', ' ')
        out.append({'value': value, 'raw': raw + suffix,
                    'decimals': _decimals(raw), 'context': ctx.strip()})
    return out


def _source_values(source_text: str) -> list[float]:
    """All numeric values in the source, deduped, capped for the pair scan."""
    vals: set[float] = set()
    for m in _NUM_RE.finditer(source_text or ''):
        v = _parse_number(m.group(1))
        if v is not None:
            vals.add(v)
    return sorted(vals)[:_MAX_SOURCE_NUMBERS]


def _number_traces(n: float, decimals: int, src: list[float]) -> bool:
    """True when ``n`` is provable from the source values, literally or derived.

    Channels (owner hole #4 — literal-only is NOT enough):
      literal  — some source number equals n within half of n's last unit;
      scale    — percent↔decimal conversion (0.863 ↔ 86.3);
      diff     — |a−b| ≈ n          (percentage-point differences);
      change   — |a−b|/|b|×100 ≈ n  (relative improvement in %);
      ratio    — a/b ≈ n or b/a ≈ n (multipliers "2 倍").
    """
    tol = max(0.5 * (10 ** (-decimals)), 1e-9)
    for s in src:
        if abs(s - n) <= tol:
            return True
        if abs(s * 100 - n) <= tol or abs(s / 100 - n) <= tol:
            return True
    for i, a in enumerate(src):
        for b in src:
            if a is b:
                continue
            if abs(abs(a - b) - n) <= tol:
                return True
            if b != 0:
                if abs(abs(a - b) / abs(b) * 100 - n) <= tol:
                    return True
                if abs(a / b - n) <= tol or abs(b / a - n) <= tol:
                    return True
    return False


def check_number_provenance(script_text: str, source_text: str,
                            *, max_issues: int = 10) -> list[str]:
    """Flag data numbers in the script that no source channel can prove."""
    src = _source_values(source_text)
    if not src:
        return []  # nothing to check against — fail open, critic covers it
    issues: list[str] = []
    seen_values: set[float] = set()
    for item in extract_data_numbers(script_text):
        n, dec = item['value'], item['decimals']
        if n in seen_values:
            continue
        seen_values.add(n)
        if not _number_traces(n, dec, src):
            issues.append(
                f'数字 "{item["raw"]}" 在素材中找不到依据(含派生通道:差值/'
                f'相对变化/倍数)——上下文:"…{item["context"]}…"')
            if len(issues) >= max_issues:
                break
    return issues


# ── 3. Structure ─────────────────────────────────────────────────────────


def check_structure(script: dict, manifest_files: list[str]) -> list[str]:
    """Enforce the listening skeleton + figure_ref whitelist.

    Hard requirements: non-empty segments; first = cold_open containing at
    least one digit; last = recap; ≥3 body segments; known section names;
    ≤MAX_FIGURE_SEGMENTS figure segments whose figure_ref copies a manifest
    filename verbatim (no invented figures).
    """
    issues: list[str] = []
    segs = (script or {}).get('segments')
    if not isinstance(segs, list) or not segs:
        return ['segments 为空或不是数组']
    known = set(SCRIPT_SECTIONS)
    first, last = segs[0], segs[-1]
    if first.get('section') != 'cold_open':
        issues.append(f'第一段必须是 cold_open(实际:"{first.get("section")}")')
    elif not re.search(r'\d', first.get('text') or ''):
        issues.append('cold_open 缺少具体数字钩子(30 秒内必须给出最亮的数字)')
    if last.get('section') != 'recap':
        issues.append(f'最后一段必须是 recap(实际:"{last.get("section")}")')
    elif len((last.get('text') or '').strip()) < 80:
        issues.append('recap 过短(<80 字)——三条"带走"各一句话')
    body = [s for s in segs[1:-1] if s.get('section') != 'roadmap']
    if len(body) < 3:
        issues.append(f'正文段落不足(仅 {len(body)} 段,至少 3 段)')
    allowed_files = set(manifest_files or [])
    fig_refs: list[str] = []
    for i, seg in enumerate(segs):
        sec = seg.get('section')
        if sec not in known:
            issues.append(f'第 {i} 段未知 section:"{sec}"')
        tlen = len((seg.get('text') or '').strip())
        if tlen < 40:
            issues.append(f'第 {i} 段({sec})过短({tlen} 字)——每段至少 40 字')
        ref = seg.get('figure_ref')
        if ref:
            fig_refs.append(ref)
            if ref not in allowed_files:
                issues.append(
                    f'第 {i} 段 figure_ref "{ref}" 不在图片清单中(不许虚构图片)')
    if len(fig_refs) > MAX_FIGURE_SEGMENTS:
        issues.append(f'讲图段落 {len(fig_refs)} 个,超过上限 {MAX_FIGURE_SEGMENTS}')
    if len(set(fig_refs)) != len(fig_refs):
        issues.append('同一张图被讲了两次(figure_ref 重复)')
    return issues


# ── 4. Duration ──────────────────────────────────────────────────────────

_CJK_RE = re.compile(r'[一-鿿　-？＀-￯]')
_LATIN_WORD_RE = re.compile(r'[A-Za-z0-9]+')

#: spoken-rate model: zh ≈ 250 chars/min, latin ≈ 155 words/min.
_ZH_CHARS_PER_MIN = 250.0
_EN_WORDS_PER_MIN = 155.0


def estimate_seconds(text: str) -> float:
    """Estimate spoken duration for mixed zh/en text (rate model above)."""
    cjk = len(_CJK_RE.findall(text or ''))
    latin_words = len(_LATIN_WORD_RE.findall(text or ''))
    return (cjk / _ZH_CHARS_PER_MIN + latin_words / _EN_WORDS_PER_MIN) * 60.0


def check_duration(script: dict, mode: str, lang: str) -> list[str]:
    """Estimated total duration must land inside the mode's ±20% band."""
    target, lo, hi = PODCAST_MODES.get(mode, PODCAST_MODES['short'])
    total = sum(estimate_seconds((s or {}).get('text') or '')
                for s in (script or {}).get('segments') or [])
    if not (lo <= total <= hi):
        return [
            f'预估时长 {total:.0f}s 超出 {mode} 档要求 [{lo}, {hi}]s'
            f'(目标 {target}s ±20%;{"压缩" if total > hi else "扩写"})'
        ]
    return []


# ── Aggregate ────────────────────────────────────────────────────────────


def validate_script(script: dict, *, mode: str, lang: str, source_text: str,
                    manifest_files: list[str]) -> list[str]:
    """Run every gate; return the aggregated issue list (empty = pass)."""
    segs = (script or {}).get('segments') or []
    full_text = '\n'.join((s or {}).get('text') or '' for s in segs)
    issues: list[str] = []
    issues += check_latex_residue(full_text)
    issues += check_unicode_math(full_text)
    issues += check_abbreviations(full_text, lang)
    issues += check_number_provenance(full_text, source_text)
    issues += check_structure(script, manifest_files)
    issues += check_duration(script, mode, lang)
    if issues:
        logger.info('[Paper:Podcast:Validate] %d issue(s): %s',
                    len(issues), ' | '.join(i[:60] for i in issues[:5]))
    return issues


__all__ = [
    'MATH_SYMBOLS',
    'check_latex_residue',
    'check_unicode_math',
    'check_abbreviations',
    'extract_data_numbers',
    'check_number_provenance',
    'check_structure',
    'estimate_seconds',
    'check_duration',
    'validate_script',
]
