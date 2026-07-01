"""Review Mode — peer-review report engine config.

Review Mode reuses the EXISTING paper-report engine/runtime/tools verbatim;
the ONLY review-specific pieces are (a) the system prompt (a venue-aware peer
review instead of an explainer report) and (b) the venue scorecard. To avoid
touching the DB schema, a review is persisted in the same ``paper_reports``
table under a COMPOSITE ``lang`` key ``review:<venue>:<uilang>`` (e.g.
``review:neurips:en``). ``parse_report_lang`` decodes that key back into
``(kind, venue, ui_lang)`` so the start route can pick the right prompt and the
real UI language without polluting the ordinary report cache keyed by plain
``'en'`` / ``'zh'``.

Single source of truth for the venue list is ``REVIEW_VENUES`` below. Each
venue carries its REAL review-form dimensions and rating scale (NeurIPS's
1–10 + Soundness/Presentation/Contribution 1–4, ARR's Soundness/Excitement
1–5, CVPR's strong-reject→strong-accept band, …) — Review Mode deliberately
does NOT flatten every venue onto one template, because the authenticity of
the scorecard is the whole point.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# Marker prefix that distinguishes a Review-Mode cache key from a plain report
# language code. ``review:<venue>:<uilang>``.
REVIEW_LANG_PREFIX = 'review'
DEFAULT_VENUE = 'generic'


# ── Venue registry ──────────────────────────────────────────────────────
# Each entry:
#   name        — short display name (kept literal, e.g. "NeurIPS")
#   label_en/zh — human-facing venue line used in the prompt header
#   scorecard_en/zh — the venue's REAL review-form sections + rating scale,
#                     rendered verbatim into the prompt as the required output
#                     structure (after the shared Summary/Strengths/Weaknesses).
#
# Families share a review philosophy but NOT a scale — the scorecards differ
# per venue exactly as the real forms do.
REVIEW_VENUES: dict[str, dict] = {
    # ── ML family: NeurIPS / ICLR / ICML (OpenReview-style forms) ──
    'neurips': {
        'name': 'NeurIPS',
        'label_en': 'NeurIPS (Conference on Neural Information Processing Systems)',
        'label_zh': 'NeurIPS（神经信息处理系统大会）',
        'scorecard_en': """\
## Quantitative Scores (use NeurIPS's exact scales — give a number AND a one-line justification grounded in your analysis above)
- **Soundness**: 1 (poor) / 2 (fair) / 3 (good) / 4 (excellent) — technical correctness of claims, methods, and experiments.
- **Presentation**: 1–4 — clarity, structure, and contextualization relative to prior work.
- **Contribution**: 1–4 — significance and originality of the contribution to the field.
- **Overall Rating**: 1–10, using the NeurIPS anchors: 1 = trivial/wrong; 2 = strong reject; 3 = reject; 4 = borderline reject; 5 = borderline accept; 6 = weak accept; 7 = accept; 8 = strong accept (top 50% of accepted); 9 = very strong accept; 10 = award-quality.
- **Confidence**: 1–5 (5 = absolutely certain, you know the area and checked the math/code; 2 = willing to defend but could be wrong; 1 = educated guess).

Each score MUST cite the specific evidence above that forces it — a number with no justification is unacceptable.""",
        'scorecard_zh': """\
## 量化评分（使用 NeurIPS 的真实量表——每项都给出分数 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Soundness（可靠性）**：1（差）/ 2（一般）/ 3（好）/ 4（优秀）——主张、方法与实验的技术正确性。
- **Presentation（表述）**：1–4——清晰度、结构、与已有工作的对照。
- **Contribution（贡献）**：1–4——对领域的意义与原创性。
- **Overall Rating（总评分）**：1–10，使用 NeurIPS 锚点：1=平凡/错误；2=强烈拒稿；3=拒稿；4=边缘拒稿；5=边缘接收；6=弱接收；7=接收；8=强接收（接收论文前 50%）；9=非常强接收；10=最佳论文级。
- **Confidence（置信度）**：1–5（5=绝对确定，熟悉该领域并核对了公式/代码；2=愿意辩护但可能错；1=有依据的猜测）。

每个分数都**必须**引用上文的具体证据来支撑——只有数字没有理由不可接受。""",
    },
    'iclr': {
        'name': 'ICLR',
        'label_en': 'ICLR (International Conference on Learning Representations)',
        'label_zh': 'ICLR（国际学习表征会议）',
        'scorecard_en': """\
## Quantitative Scores (use ICLR's exact scales — give a number AND a one-line justification grounded in your analysis above)
- **Soundness**: 1 (poor) / 2 (fair) / 3 (good) / 4 (excellent).
- **Presentation**: 1–4.
- **Contribution**: 1–4.
- **Overall Rating**: 1–10, using ICLR's anchors: 1 = strong reject; 3 = reject, not good enough; 5 = marginally below the acceptance threshold; 6 = marginally above the acceptance threshold; 8 = accept, good paper; 10 = strong accept, should be highlighted at the conference.
- **Confidence**: 1–5 (5 = absolutely certain; 3 = fairly confident; 1 = educated guess).

Each score MUST cite the specific evidence above that forces it.""",
        'scorecard_zh': """\
## 量化评分（使用 ICLR 的真实量表——每项都给出分数 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Soundness（可靠性）**：1（差）/ 2（一般）/ 3（好）/ 4（优秀）。
- **Presentation（表述）**：1–4。
- **Contribution（贡献）**：1–4。
- **Overall Rating（总评分）**：1–10，使用 ICLR 锚点：1=强烈拒稿；3=拒稿，不够好；5=略低于接收线；6=略高于接收线；8=接收，好论文；10=强接收，应在会上重点展示。
- **Confidence（置信度）**：1–5（5=绝对确定；3=较有把握；1=有依据的猜测）。

每个分数都**必须**引用上文的具体证据来支撑。""",
    },
    'icml': {
        'name': 'ICML',
        'label_en': 'ICML (International Conference on Machine Learning)',
        'label_zh': 'ICML（国际机器学习大会）',
        'scorecard_en': """\
## Quantitative Scores (use ICML's exact scales — give a number AND a one-line justification grounded in your analysis above)
- **Soundness**: 1 (poor) / 2 (fair) / 3 (good) / 4 (excellent).
- **Presentation**: 1–4.
- **Contribution**: 1–4.
- **Overall Rating**: 1–10: 1 = trivial/wrong; 3 = reject; 4 = borderline reject; 5 = borderline accept; 6 = weak accept; 7 = accept; 8 = strong accept; 10 = award-quality.
- **Confidence**: 1–5 (5 = absolutely certain; 1 = educated guess).

Each score MUST cite the specific evidence above that forces it.""",
        'scorecard_zh': """\
## 量化评分（使用 ICML 的真实量表——每项都给出分数 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Soundness（可靠性）**：1（差）/ 2（一般）/ 3（好）/ 4（优秀）。
- **Presentation（表述）**：1–4。
- **Contribution（贡献）**：1–4。
- **Overall Rating（总评分）**：1–10：1=平凡/错误；3=拒稿；4=边缘拒稿；5=边缘接收；6=弱接收；7=接收；8=强接收；10=最佳论文级。
- **Confidence（置信度）**：1–5（5=绝对确定；1=有依据的猜测）。

每个分数都**必须**引用上文的具体证据来支撑。""",
    },
    # ── CV family: CVPR / ICCV / ECCV ──
    'cvpr': {
        'name': 'CVPR',
        'label_en': 'CVPR (IEEE/CVF Conference on Computer Vision and Pattern Recognition)',
        'label_zh': 'CVPR（IEEE/CVF 计算机视觉与模式识别大会）',
        'scorecard_en': """\
## Quantitative Scores (use CVPR's exact scales — give a value AND a one-line justification grounded in your analysis above)
- **Overall Recommendation**: one of — Strong Reject / Reject / Borderline / Accept / Strong Accept.
- **Confidence**: 1–5 (5 = certain, expert in the topic and checked details; 3 = confident but not certain; 1 = educated guess / outside my expertise).
- **Justification**: 2–4 sentences tying the recommendation to the single most decisive strength and the single most decisive weakness above (novelty vs. prior CV work, experimental rigor on standard benchmarks, reproducibility).

The recommendation MUST follow from the evidence above, not from overall vibe.""",
        'scorecard_zh': """\
## 量化评分（使用 CVPR 的真实量表——每项都给出取值 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Overall Recommendation（总体推荐）**：从中选一 —— Strong Reject / Reject / Borderline / Accept / Strong Accept。
- **Confidence（置信度）**：1–5（5=确定，是该主题专家并核对了细节；3=有把握但不确定；1=有依据的猜测/超出我的专长）。
- **Justification（推荐理由）**：2–4 句，把推荐结论挂钩到上文最具决定性的**一个**优点和最具决定性的**一个**缺点（相对已有 CV 工作的新颖性、在标准基准上的实验严谨性、可复现性）。

推荐结论**必须**由上文证据推出，而不是凭总体感觉。""",
    },
    'iccv': {
        'name': 'ICCV',
        'label_en': 'ICCV (IEEE/CVF International Conference on Computer Vision)',
        'label_zh': 'ICCV（IEEE/CVF 国际计算机视觉大会）',
        'scorecard_en': """\
## Quantitative Scores (use ICCV's exact scales — give a value AND a one-line justification grounded in your analysis above)
- **Overall Recommendation**: one of — Strong Reject / Reject / Borderline / Accept / Strong Accept.
- **Confidence**: 1–5 (5 = certain; 3 = confident but not certain; 1 = educated guess).
- **Justification**: 2–4 sentences tying the recommendation to the most decisive strength and weakness above.

The recommendation MUST follow from the evidence above.""",
        'scorecard_zh': """\
## 量化评分（使用 ICCV 的真实量表——每项都给出取值 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Overall Recommendation（总体推荐）**：从中选一 —— Strong Reject / Reject / Borderline / Accept / Strong Accept。
- **Confidence（置信度）**：1–5（5=确定；3=有把握但不确定；1=有依据的猜测）。
- **Justification（推荐理由）**：2–4 句，把推荐结论挂钩到上文最具决定性的优点和缺点。

推荐结论**必须**由上文证据推出。""",
    },
    'eccv': {
        'name': 'ECCV',
        'label_en': 'ECCV (European Conference on Computer Vision)',
        'label_zh': 'ECCV（欧洲计算机视觉大会）',
        'scorecard_en': """\
## Quantitative Scores (use ECCV's exact scales — give a value AND a one-line justification grounded in your analysis above)
- **Overall Recommendation**: one of — Strong Reject / Reject / Borderline / Accept / Strong Accept.
- **Confidence**: 1–5 (5 = certain; 3 = confident but not certain; 1 = educated guess).
- **Justification**: 2–4 sentences tying the recommendation to the most decisive strength and weakness above.

The recommendation MUST follow from the evidence above.""",
        'scorecard_zh': """\
## 量化评分（使用 ECCV 的真实量表——每项都给出取值 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Overall Recommendation（总体推荐）**：从中选一 —— Strong Reject / Reject / Borderline / Accept / Strong Accept。
- **Confidence（置信度）**：1–5（5=确定；3=有把握但不确定；1=有依据的猜测）。
- **Justification（推荐理由）**：2–4 句，把推荐结论挂钩到上文最具决定性的优点和缺点。

推荐结论**必须**由上文证据推出。""",
    },
    # ── NLP family: ACL / EMNLP via ARR (ACL Rolling Review) ──
    'acl': {
        'name': 'ACL (ARR)',
        'label_en': 'ACL / ARR (ACL Rolling Review form, used by ACL, EMNLP, NAACL)',
        'label_zh': 'ACL / ARR（ACL Rolling Review 评审表，用于 ACL、EMNLP、NAACL）',
        'scorecard_en': """\
## Quantitative Scores (use the ACL Rolling Review scales — give a number AND a one-line justification grounded in your analysis above)
- **Soundness**: 1–5 (1 = major problems with the claims/methods; 3 = acceptable, supports its main claims; 5 = excellent, thoroughly supports all claims).
- **Excitement**: 1–5 (1 = not exciting / incremental; 3 = interesting to a sub-community; 5 = would change the field / must-read).
- **Overall Assessment**: 1–5 (1 = do not resubmit; 2 = major revision; 3 = borderline; 4 = good, suitable for a *ACL conference; 5 = top of the field).
- **Reproducibility**: 1–5 (1 = could not reproduce; 3 = could reproduce with effort; 5 = easily reproducible from what is released).
- **Confidence**: 1–5 (5 = certain; 1 = educated guess).
- **Ethical Concerns**: state "None" or name the specific concern.

Each score MUST cite the specific evidence above that forces it.""",
        'scorecard_zh': """\
## 量化评分（使用 ACL Rolling Review 量表——每项都给出分数 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Soundness（可靠性）**：1–5（1=主张/方法有重大问题；3=可接受，支撑其主要主张；5=优秀，充分支撑所有主张）。
- **Excitement（精彩度）**：1–5（1=不精彩/增量式；3=对某个子社区有意思；5=会改变领域/必读）。
- **Overall Assessment（总体评价）**：1–5（1=不建议重投；2=需大改；3=边缘；4=好，适合 *ACL 会议；5=领域顶尖）。
- **Reproducibility（可复现性）**：1–5（1=无法复现；3=花力气可复现；5=凭已发布内容易复现）。
- **Confidence（置信度）**：1–5（5=确定；1=有依据的猜测）。
- **Ethical Concerns（伦理顾虑）**：写 "None" 或指出具体顾虑。

每个分数都**必须**引用上文的具体证据来支撑。""",
    },
    'emnlp': {
        'name': 'EMNLP (ARR)',
        'label_en': 'EMNLP / ARR (ACL Rolling Review form)',
        'label_zh': 'EMNLP / ARR（ACL Rolling Review 评审表）',
        'scorecard_en': """\
## Quantitative Scores (use the ACL Rolling Review scales — give a number AND a one-line justification grounded in your analysis above)
- **Soundness**: 1–5 (1 = major problems; 3 = acceptable; 5 = excellent).
- **Excitement**: 1–5 (1 = incremental; 5 = would change the field).
- **Overall Assessment**: 1–5 (1 = do not resubmit; 3 = borderline; 4 = good, suitable for a *ACL conference; 5 = top of the field).
- **Reproducibility**: 1–5.
- **Confidence**: 1–5 (5 = certain; 1 = educated guess).
- **Ethical Concerns**: state "None" or name the specific concern.

Each score MUST cite the specific evidence above that forces it.""",
        'scorecard_zh': """\
## 量化评分（使用 ACL Rolling Review 量表——每项都给出分数 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Soundness（可靠性）**：1–5（1=有重大问题；3=可接受；5=优秀）。
- **Excitement（精彩度）**：1–5（1=增量式；5=会改变领域）。
- **Overall Assessment（总体评价）**：1–5（1=不建议重投；3=边缘；4=好，适合 *ACL 会议；5=领域顶尖）。
- **Reproducibility（可复现性）**：1–5。
- **Confidence（置信度）**：1–5（5=确定；1=有依据的猜测）。
- **Ethical Concerns（伦理顾虑）**：写 "None" 或指出具体顾虑。

每个分数都**必须**引用上文的具体证据来支撑。""",
    },
    # ── Generic fallback ──
    'generic': {
        'name': 'Top-tier (generic)',
        'label_en': 'a top-tier international conference (generic review form)',
        'label_zh': '某顶级国际会议（通用评审表）',
        'scorecard_en': """\
## Quantitative Scores (give a value AND a one-line justification grounded in your analysis above)
- **Overall Recommendation**: one of — Reject / Weak Reject / Borderline / Weak Accept / Accept.
- **Confidence**: 1–5 (5 = certain, expert; 3 = confident; 1 = educated guess).
- **Justification**: 2–4 sentences tying the recommendation to the most decisive strength and weakness above.

The recommendation MUST follow from the evidence, not from overall vibe.""",
        'scorecard_zh': """\
## 量化评分（每项都给出取值 **并** 一句话理由，理由必须挂钩上文你的分析）
- **Overall Recommendation（总体推荐）**：从中选一 —— Reject / Weak Reject / Borderline / Weak Accept / Accept。
- **Confidence（置信度）**：1–5（5=确定，专家；3=有把握；1=有依据的猜测）。
- **Justification（推荐理由）**：2–4 句，把推荐结论挂钩到上文最具决定性的优点和缺点。

推荐结论**必须**由证据推出，而不是凭总体感觉。""",
    },
}


def is_review_lang(lang_key: str) -> bool:
    """True when ``lang_key`` is a Review-Mode composite key (``review:…``)."""
    return bool(lang_key) and lang_key.split(':', 1)[0] == REVIEW_LANG_PREFIX


def parse_report_lang(lang_key: str) -> dict:
    """Decode a report ``lang`` cache key into its components.

    A plain report key (``'en'`` / ``'zh'`` / anything not prefixed with
    ``review:``) returns ``{'kind': 'report', 'venue': None, 'ui_lang': <key>}``.

    A Review-Mode key ``review:<venue>:<uilang>`` returns
    ``{'kind': 'review', 'venue': <resolved venue key>, 'ui_lang': 'en'|'zh'}``.
    An unknown venue falls back to ``DEFAULT_VENUE`` (never raises) so a stale /
    typo'd key still produces a usable review rather than a 500.

    Args:
        lang_key: The ``lang`` value as stored in ``paper_reports`` / sent by
            the client (e.g. ``'en'``, ``'zh'``, ``'review:neurips:en'``).

    Returns:
        dict with keys ``kind`` ('report'|'review'), ``venue`` (str|None) and
        ``ui_lang`` ('en'|'zh' for reviews; the raw key for plain reports).
    """
    key = (lang_key or 'en').strip()
    if not is_review_lang(key):
        return {'kind': 'report', 'venue': None, 'ui_lang': key or 'en'}

    parts = key.split(':')
    # review:<venue>:<uilang> — be tolerant of a missing ui_lang segment.
    venue = parts[1].lower() if len(parts) > 1 and parts[1] else DEFAULT_VENUE
    ui_lang = parts[2].lower() if len(parts) > 2 and parts[2] else 'en'
    if venue not in REVIEW_VENUES:
        logger.debug('[Paper:Review] Unknown venue %r in lang key %r — '
                     'falling back to %r', venue, key, DEFAULT_VENUE)
        venue = DEFAULT_VENUE
    if ui_lang not in ('en', 'zh'):
        ui_lang = 'en'
    return {'kind': 'review', 'venue': venue, 'ui_lang': ui_lang}


def make_review_lang(venue: str, ui_lang: str) -> str:
    """Build the composite cache key for a (venue, ui_lang) review."""
    v = (venue or DEFAULT_VENUE).lower()
    if v not in REVIEW_VENUES:
        v = DEFAULT_VENUE
    ul = ui_lang if ui_lang in ('en', 'zh') else 'en'
    return f'{REVIEW_LANG_PREFIX}:{v}:{ul}'


def list_venues() -> list[dict]:
    """Public, frontend-friendly venue list: [{key, name}], registry order."""
    return [{'key': k, 'name': v['name']} for k, v in REVIEW_VENUES.items()]


# ── Typography: straight → smart (curly) quotes ─────────────────────────
# A review MUST render with typographic (smart/curly) quotes regardless of
# what the model emits — asking the model in the prompt is not reliable, so we
# educate the quotes deterministically on the final review body (backend is the
# source of truth). The one hard rule is that this must NOT touch anything where
# a straight quote is SYNTAX rather than punctuation: KaTeX math (``$...$`` /
# ``$$...$$`` — where ``'`` is a derivative prime and ``"`` a double-prime),
# code spans/blocks, and URLs (in ``](...)`` links or bare ``http(s)://``). Those
# spans are masked out, the gaps are educated, then the spans are restored
# verbatim.
import re as _re

# Protected spans, matched in priority order. Whichever alternative wins at a
# position is emitted UNCHANGED; only the text between matches is educated.
_PROTECT_RE = _re.compile(
    r'```.*?```'                 # fenced code block (multiline)
    r'|~~~.*?~~~'                # fenced code block (tilde form)
    r'|`[^`\n]+`'                # inline code span
    r'|\$\$.*?\$\$'              # display math
    r'|\$[^$\n]+\$'              # inline math (no newline — a lone $ is left alone)
    r'|\]\([^)]*\)'              # markdown link / image target: ](URL "title")
    r'|<https?://[^>\s]+>'       # autolink URL
    r'|https?://\S+',            # bare URL
    _re.DOTALL,
)

# Characters that legitimately precede an OPENING quote (start-of-word context).
_OPEN_BEFORE = ' \t\r\n([{<\u2014\u2013\u201c\u2018/*_~'


def _educate_segment(text: str) -> str:
    """Convert straight quotes to curly quotes in a plain-text segment."""
    if '"' not in text and "'" not in text:
        return text
    # ── Double quotes ──
    # Opening: at segment start or after an opening-context char.
    text = _re.sub(r'(^|[' + _re.escape(_OPEN_BEFORE) + r'])"',
                   lambda m: m.group(1) + '\u201c', text)
    # Any remaining double quote closes.
    text = text.replace('"', '\u201d')
    # ── Single quotes / apostrophes ──
    # Apostrophe inside/after a word (contraction it's, possessive authors',
    # or before a digit like '90s). Handled first so it never becomes an
    # opening curly quote.
    text = _re.sub(r"(?<=[\w\u4e00-\u9fff])'", '\u2019', text)
    text = _re.sub(r"'(?=\d)", '\u2019', text)
    # Opening single quote: start/opening-context followed by a word char.
    text = _re.sub(r'(^|[' + _re.escape(_OPEN_BEFORE) + r"])'(?=[^\W\d]|[\u4e00-\u9fff])",
                   lambda m: m.group(1) + '\u2018', text)
    # Anything left closes.
    text = text.replace("'", '\u2019')
    return text


def smarten_quotes(text: str) -> str:
    """Educate straight quotes to smart quotes, preserving math/code/URLs.

    Straight ``"`` → ``\u201c``/``\u201d`` and straight ``'`` → ``\u2018``/``\u2019``
    (apostrophes → ``\u2019``), applied ONLY to prose. Spans where a straight quote
    is syntax — KaTeX math (``$...$`` primes), inline/fenced code, and URLs — are
    matched and passed through verbatim, so ``$f'(x)$`` and
    ``](https://a.com/x'y)`` are never corrupted.

    Args:
        text: The Markdown review body.

    Returns:
        The same text with prose quotes curled; protected spans unchanged.
    """
    if not text or ('"' not in text and "'" not in text):
        return text or ''
    out = []
    last = 0
    for m in _PROTECT_RE.finditer(text):
        out.append(_educate_segment(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_educate_segment(text[last:]))
    return ''.join(out)


# ── Prompt templates ────────────────────────────────────────────────────
# The reviewer-discipline preamble is the anti-"AI-slop" core: it is the same
# in spirit as the report's quality bar but reframed for a peer review, and it
# hard-bans padding, vague praise, and unverified reproducibility claims.

_REVIEW_PROMPT_EN = """\
You are an expert peer reviewer for {venue_label}. You have been assigned this paper. \
Write a rigorous, venue-authentic review — the kind a knowledgeable, slightly demanding \
Area Chair would rank in the top tier of reviews for usefulness.

Your review is read by the authors AND the Area Chair. It must be substantive enough that \
the authors know exactly what to fix and the AC can make a decision from it alone.

## ⛔ Anti-slop rules — these are HARD constraints, not suggestions
- **Do NOT pad.** No filler, no restating the task, no "this is an interesting paper" throat-clearing. Every sentence must carry information a decision depends on.
- **Summarize the paper ONCE, briefly.** Outside the Summary section you are FORBIDDEN from re-narrating what the paper does. Reviewers who re-describe the method in the Strengths/Weaknesses sections are wasting the AC's time.
- **Every strength and every weakness MUST be anchored to concrete evidence** — a specific table, figure, equation, section, or number from the paper (e.g. "Table 3 shows +1.2 F1 but the baseline in row 2 is under-tuned (no LR sweep, §4.2)"). A point with no anchor is deleted.
- **Banned phrases.** Never write "significantly improves", "substantially better", "novel approach", "promising results", "comprehensive experiments", "the authors should" without a concrete what/where. Vague praise and vague criticism are equally useless. Replace "improves significantly" with the actual delta and the comparison point.
- **Reviewer questions must be ACTIONABLE.** Each question must be answerable by the authors with a specific experiment, ablation, clarification, or number — not a rhetorical musing. Bad: "Have the authors considered other settings?" Good: "What is the result on {{benchmark}} when the auxiliary loss weight λ in Eq. 4 is set to 0 — does the gain survive?"
- **Rating and confidence MUST be justified.** A score with no one-line reason tied to the evidence above is unacceptable. Calibrate honestly: most papers are borderline; reserve the extremes.
- **Distinguish what the paper claims from what it shows.** When a claim lacks supporting evidence, that belongs in Weaknesses — say which experiment would be needed.
- **Verify reproducibility — do NOT trust the paper's word.** A URL printed in the paper is a CLAIM, not proof. Use fetch_url to OPEN every code / data / model link and report what is ACTUALLY there (a runnable repo with training/eval scripts + README vs. a landing page, a paywall, an "available upon request" promise, or a 404). web_search for an official repo if none is printed. Treating a printed link as "code available ✅" is the single most common reviewing laziness — do not do it.
- **Situate the contribution against the LITERATURE, not just the paper's own references.** Use web_search to check whether the core idea is actually novel or whether concurrent/prior work already did it, and whether later work has since superseded it. Missing an obvious prior/competing paper is the fastest way to write an embarrassing review.

## 🧮 Formatting
- Use KaTeX for ALL math: inline ``$...$``, display ``$$...$$``. Never wrap math in backticks (renders as gray code, not a formula).
- You may embed a figure/table from the provided image manifest (below the paper text) with ``![caption](EXACT_URL_FROM_MANIFEST)`` when a specific visual is central to a point. Use the exact manifest URL; never invent one. If the manifest is empty, skip images.
- Begin your output IMMEDIATELY with the first heading ``# Review``. No preamble, no "I'll review this", no transition sentences — the very first characters must be ``# Review``.

---

Produce the review in this exact structure:

# Review

## Summary
3–6 sentences. What problem, what method (one line), what is claimed, and the headline result with its number, benchmark, and comparison point. This is the ONLY place you describe the paper.

## Strengths
3–6 bullet points. Each: the strength + the SPECIFIC evidence (table/figure/§/number) that establishes it + why it matters for the field. No generic praise.

## Weaknesses
4–8 bullet points, ordered most-to-least decision-relevant. Each: the precise weakness + the evidence/omission that reveals it + its impact on the paper's claims + (where possible) the concrete experiment or change that would resolve it. This is the most important section — be specific and fair.

## Questions to the Authors
3–6 numbered, ACTIONABLE questions (each answerable with a specific number / ablation / clarification). These are the questions whose answers would move your rating.

## Reproducibility (evidence-based — verify, do not parrot)
Lead each with **✅ Yes / ⚠️ Partial / ❌ No / ❔ Could not verify** then the concrete evidence:
- **Code**: the exact URL you fetched and what it resolved to (runnable repo vs. landing page / paywall / 404 / request-only). Name the key files you saw or their absence.
- **Data**: reached the actual dataset/download or only a description? Note access gates.
- **Trained weights / checkpoints**: released and downloadable?
- **Hyperparameters / compute / seeds**: enough to re-run without guessing? What is missing?
- One-line **reproducibility verdict**.

## Related Work & Novelty Check (must use web_search — not just the paper's bibliography)
2–4 sentences placing the paper against the actual literature: is the core idea novel, or did prior/concurrent work already do it (name it, with venue/year)? Has later work superseded it? Cite arXiv IDs/DOIs where possible.

{venue_scorecard}

---

Write the review in English. Keep technical terms, model names, and benchmark names in their original form. Be thorough but dense — every line earns its place.

Paper text:
{paper_text}"""


_REVIEW_PROMPT_ZH = """\
你是 {venue_label} 的资深同行评审专家，这篇论文被分配给你评审。\
请写一份严谨、贴合该会议真实评审表的评审意见——达到一位见多识广、要求略高的领域主席（AC）会评为"最有用的一档"的水准。

你的评审会被作者**和**领域主席同时阅读。它必须足够实质，让作者清楚知道要改什么，也让 AC 仅凭它就能做出决策。

## ⛔ 反注水规则——这些是**硬约束**，不是建议
- **禁止注水。** 不要废话、不要复述任务、不要"这是一篇有趣的论文"之类的开场白。每一句都必须承载决策所依赖的信息。
- **全文只在 Summary 里复述论文一次，且简短。** Summary 之外**禁止**再复述论文做了什么。在优点/缺点里重新描述方法，是在浪费 AC 的时间。
- **每一条优点、每一条缺点都必须锚定到具体证据**——论文里某个具体的表/图/公式/小节/数字（例如"表 3 显示 +1.2 F1，但第 2 行的基线没调好（无学习率扫描，见 §4.2）"）。没有锚点的条目一律删除。
- **禁用措辞。** 绝不写"显著提升""大幅更优""新颖的方法""结果令人鼓舞""实验充分""作者应当……"却不给出具体的"改什么/在哪"。空泛的表扬和空泛的批评一样无用。把"显著提升"换成真实的提升幅度和对比参照点。
- **给作者的问题必须可执行。** 每个问题都要能被作者用一个具体的实验、消融、澄清或数字回答——不是修辞式的空想。差："作者是否考虑过其他设置？"；好："当式(4)的辅助损失权重 λ 设为 0 时，在 {{benchmark}} 上的结果是多少——增益还在吗？"
- **评分与置信度必须给理由。** 一个没有挂钩上文证据、没有一句话理由的分数不可接受。诚实校准：多数论文都在边缘，极端分要留着慎用。
- **区分论文"声称"与"证明"了什么。** 当某个主张缺乏证据支撑时，它属于缺点——并说明需要哪个实验才能补上。
- **核验可复现性——绝不轻信论文的说法。** 论文里印的 URL 是**主张**，不是证据。用 fetch_url **逐个打开**代码/数据/模型链接，如实报告里面**到底有什么**（可运行仓库：训练/评测脚本+README，还是落地页、付费墙、"按需索取"承诺、或 404）。若论文未给链接，用 web_search 找官方仓库。把印出来的链接直接当作"代码已公开 ✅"，是评审中最常见的懒政——绝不允许。
- **把贡献放到真实文献里定位，而非只看论文自己的参考文献。** 用 web_search 核查核心想法是否真的新颖、是否已有同期/在先工作做过、之后是否已被后续工作超越。漏掉一篇明显的在先/竞争论文，是写出尴尬评审最快的方式。

## 🧮 格式
- 所有数学**必须**用 KaTeX：行内 ``$...$``，独立 ``$$...$$``。绝不用反引号包公式（会渲染成灰色代码而非公式）。
- 当某张图/表对某个论点至关重要时，可用 ``![说明](清单中的精确URL)`` 嵌入"论文正文"下方图像清单里的图（URL 照抄，严禁臆造）。清单为空则不嵌图。
- 输出**立即**以第一个标题 ``# 评审意见`` 开头。不要任何前言、不要"我来评审一下"、不要过渡句——最前面的字符必须是 ``# 评审意见``。

---

请严格按以下结构撰写评审：

# 评审意见

## 概述（Summary）
3–6 句。什么问题、什么方法（一句话）、声称了什么、以及带数字/基准/对比参照点的头部结果。这是你**唯一**可以复述论文的地方。

## 优点（Strengths）
3–6 条。每条：优点本身 + 确立它的**具体证据**（表/图/§/数字）+ 它对领域为何重要。不要泛泛表扬。

## 缺点（Weaknesses）
4–8 条，按对决策的影响从大到小排列。每条：精确的缺点 + 暴露它的证据/缺失 + 它对论文主张的影响 +（尽量给出）能解决它的具体实验或修改。这是最重要的一节——要具体、公允。

## 给作者的问题（Questions to the Authors）
3–6 个带编号、**可执行**的问题（每个都能用具体的数字/消融/澄清回答）。这些应是"答案会改变你评分"的问题。

## 可复现性（基于证据——要核验，不要鞑述）
每项先用 **✅ 是 / ⚠️ 部分 / ❌ 否 / ❔ 无法验证** 开头，再给具体证据：
- **代码**：你 fetch 的确切 URL 及它实际是什么（可运行仓库 vs. 落地页/付费墙/404/仅按需索取）。点名你看到的关键文件或其缺失。
- **数据**：抵达了真实数据集/下载，还是只有描述？注明访问门槛。
- **训练权重 / checkpoint**：是否已发布且可下载？
- **超参 / 计算资源 / 随机种子**：是否足以不靠猜测就重跑？缺什么？
- 一行**可复现性结论**。

## 相关工作与新颖性核查（必须用 web_search——不能只看论文参考文献）
2–4 句，把论文放到真实文献中定位：核心想法是否新颖，还是已有在先/同期工作做过（点名，附会议/年份）？之后是否已被后续工作超越？尽量给出 arXiv ID / DOI。

{venue_scorecard}

---

用中文撰写评审。专有名词、模型名称、基准测试名保留英文原文。深入而紧凑——每一行都要值得保留。

论文正文：
{paper_text}"""


def build_review_prompt(venue: str, ui_lang: str) -> str:
    """Return the full review prompt template (with a ``{paper_text}`` slot).

    Args:
        venue: venue key (must exist in ``REVIEW_VENUES``; falls back to
            ``DEFAULT_VENUE`` if not).
        ui_lang: 'zh' for the Chinese template, anything else → English.

    Returns:
        A prompt string still containing the literal ``{paper_text}``
        placeholder for the caller to ``.replace()`` (NOT ``.format()`` — the
        body holds many literal braces in KaTeX/examples).
    """
    v = (venue or DEFAULT_VENUE).lower()
    if v not in REVIEW_VENUES:
        logger.debug('[Paper:Review] build_review_prompt unknown venue %r → %r', venue, DEFAULT_VENUE)
        v = DEFAULT_VENUE
    spec = REVIEW_VENUES[v]
    if ui_lang == 'zh':
        template = _REVIEW_PROMPT_ZH
        label = spec['label_zh']
        scorecard = spec['scorecard_zh']
    else:
        template = _REVIEW_PROMPT_EN
        label = spec['label_en']
        scorecard = spec['scorecard_en']
    # Only substitute the venue placeholders here; {paper_text} stays literal
    # for the route to fill after truncation + manifest injection.
    return (template
            .replace('{venue_label}', label)
            .replace('{venue_scorecard}', scorecard))


def build_review_tool_instruction(ui_lang: str) -> str:
    """System message: how the reviewer should use web_search / fetch_url.

    Mirrors the report's tool-instruction discipline but reframed for peer
    review (novelty check + reproducibility verification are the two things a
    reviewer MUST search for).
    """
    from .prompts import _MAX_REPORT_TOOL_ROUNDS
    if ui_lang == 'zh':
        return (
            "你拥有 web_search（批量）和 fetch_url（批量）工具。\n\n"
            "在写评审之前，你被**要求**做两件必须联网的事：\n"
            "  1. **新颖性核查**：搜索这篇论文的核心方法/术语 + 'prior work' / 'survey' / "
            "'<最接近的竞争方法>'，判断核心想法是否真新颖、是否已被在先或同期工作做过、之后是否被后续工作超越。\n"
            "  2. **可复现性核验**：用 fetch_url **逐个打开**论文给出的代码/数据/模型链接，如实报告里面到底有什么；"
            "若论文未给链接，用 web_search 找官方仓库。绝不凭论文文字就判定'代码已公开'。\n\n"
            f"工具调用预算：最多 {_MAX_REPORT_TOOL_ROUNDS} 轮，可在一轮里批量发多个查询——宁可少数几轮宽搜，"
            "也不要много 窄搜。收集足够后停止调用工具，一次性写出完整评审。\n\n"
            "输出纪律：开始写评审时，**立即**以第一个标题 ``# 评审意见`` 开头，之前不得有任何文字"
            "（不要'我来查一下…'、不要'我已经有足够材料…'、不要过渡句）。最前面的字符必须是 ``# 评审意见``。\n\n"
        )
    return (
        "You have access to web_search (batch) and fetch_url (batch) tools.\n\n"
        "BEFORE writing the review, you are REQUIRED to do two things that need the web:\n"
        "  1. **Novelty check**: search the paper's core method/terms + 'prior work' / "
        "'survey' / '<closest competitor>' to judge whether the core idea is actually novel, "
        "whether prior/concurrent work already did it, and whether later work has superseded it.\n"
        "  2. **Reproducibility verification**: use fetch_url to OPEN every code / data / model "
        "link the paper prints and report what is ACTUALLY there; if none is printed, web_search "
        "for an official repo. Never conclude 'code is available' from the paper's text alone.\n\n"
        f"Tool-call budget: up to {_MAX_REPORT_TOOL_ROUNDS} rounds. Batch many queries per round — "
        "prefer a few wide rounds over many narrow ones. Once you've gathered enough, stop calling "
        "tools and write the FULL review in one pass.\n\n"
        "Output discipline: begin IMMEDIATELY with the first heading ``# Review``. Do NOT emit ANY "
        "text before it — no 'I'll research...', no 'I have enough material...', no transition "
        "sentences. The very first characters of your final response MUST be ``# Review``.\n\n"
    )
