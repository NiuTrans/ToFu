"""Review Mode — venue registry + composite-key language helpers.

Single source of truth for the venue list is ``REVIEW_VENUES`` below. Each
venue carries its REAL review-form dimensions and rating scale. A review is
persisted in the same ``paper_reports`` table under a COMPOSITE ``lang`` key
``review:<venue>:<uilang>``; ``parse_report_lang`` decodes that key back into
``(kind, venue, ui_lang)``.
"""

from lib.log import get_logger

logger = get_logger(__name__)


REVIEW_LANG_PREFIX = 'review'
# Author-rebuttal follow-up. A rebuttal reuses the review venue registry and the
# SAME paper_reports table under a sibling composite key ``rebuttal:<venue>:<uilang>``
# (never collides with the review's ``review:<venue>:<uilang>`` row), so the
# follow-up reply + score-adjustment decision are independently cacheable and
# exportable. See ``build_rebuttal_prompt`` / ``parse_rebuttal_decision``.
REBUTTAL_LANG_PREFIX = 'rebuttal'
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
    'nlpcc': {
        'name': 'NLPCC',
        'label_en': 'NLPCC (CCF International Conference on Natural Language Processing and Chinese Computing, Springer LNAI, double-blind)',
        'label_zh': 'NLPCC（CCF 国际自然语言处理与中文计算会议，Springer LNAI，双盲评审）',
        'scorecard_en': """\
## Quantitative Scores (NLPCC's OpenReview form has only TWO scored fields — the Title and the Review text are the prose above; transcribe ONLY these two numbers into the form)
- **Overall Assessment (OA)**: 1–6 (1 = clear reject; 2 = reject; 3 = weak reject / borderline; 4 = weak accept / borderline; 5 = accept, good paper; 6 = strong accept, award-quality). Your judgement of soundness, novelty, clarity, and comparison to prior NLP/CC work all fold into this single number — argue them in the Review text above, not as separate scores.
- **Confidence**: 1–5 (5 = you know the area well and checked the details; 3 = fairly confident; 1 = educated guess / outside your expertise).

Each score MUST cite the specific evidence above that forces it — a number with no justification is unacceptable.""",
        'scorecard_zh': """\
## 量化评分（NLPCC 的 OpenReview 评审表只有两个打分字段——上方的标题与评审正文即散文本体；只把下面这两个数字填进表单）
- **Overall Assessment（OA，总体评价）**：1–6（1=明确拒稿；2=拒稿；3=弱拒/边缘；4=弱接收/边缘；5=接收，好论文；6=强接收，最佳论文级）。可靠性、新颖性、清晰度、与在先 NLP/CC 工作的对比，全部折进这一个数字——在上方评审正文里论证它们，而不是作为独立分数。
- **Confidence（置信度）**：1–5（5=熟悉该领域并核对了细节；3=较有把握；1=有依据的猜测/超出我的专长）。

每个分数都**必须**引用上文的具体证据来支撑——只有数字没有理由不可接受。""",
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


def is_rebuttal_lang(lang_key: str) -> bool:
    """True when ``lang_key`` is an author-rebuttal composite key (``rebuttal:…``)."""
    return bool(lang_key) and lang_key.split(':', 1)[0] == REBUTTAL_LANG_PREFIX


def is_review_family(lang_key: str) -> bool:
    """True for both Review Mode and its rebuttal follow-up.

    These share the text-only, figure-free treatment (a peer review and a
    reviewer's rebuttal reply are decision documents, not illustrated
    explainers): no image manifest, no appendix, no insight/terminology second
    pass. Callers gate that behaviour on this predicate rather than repeating
    ``is_review_lang(x) or is_rebuttal_lang(x)`` everywhere.
    """
    return is_review_lang(lang_key) or is_rebuttal_lang(lang_key)


def parse_report_lang(lang_key: str) -> dict:
    """Decode a report ``lang`` cache key into its components.

    A plain report key (``'en'`` / ``'zh'`` / anything not prefixed with
    ``review:``) returns ``{'kind': 'report', 'venue': None, 'ui_lang': <key>}``.

    A Review-Mode key ``review:<venue>:<uilang>`` returns
    ``{'kind': 'review', 'venue': <resolved venue key>, 'ui_lang': 'en'|'zh'}``;
    a rebuttal key ``rebuttal:<venue>:<uilang>`` returns the same shape with
    ``kind == 'rebuttal'``.
    An unknown venue falls back to ``DEFAULT_VENUE`` (never raises) so a stale /
    typo'd key still produces a usable result rather than a 500.

    Args:
        lang_key: The ``lang`` value as stored in ``paper_reports`` / sent by
            the client (e.g. ``'en'``, ``'zh'``, ``'review:neurips:en'``).

    Returns:
        dict with keys ``kind`` ('report'|'review'), ``venue`` (str|None) and
        ``ui_lang`` ('en'|'zh' for reviews; the raw key for plain reports).
    """
    key = (lang_key or 'en').strip()
    if not is_review_family(key):
        return {'kind': 'report', 'venue': None, 'ui_lang': key or 'en'}

    parts = key.split(':')
    # <review|rebuttal>:<venue>:<uilang> — tolerant of a missing ui_lang segment.
    kind = 'rebuttal' if parts[0] == REBUTTAL_LANG_PREFIX else 'review'
    venue = parts[1].lower() if len(parts) > 1 and parts[1] else DEFAULT_VENUE
    ui_lang = parts[2].lower() if len(parts) > 2 and parts[2] else 'en'
    if venue not in REVIEW_VENUES:
        logger.debug('[Paper:Review] Unknown venue %r in lang key %r — '
                     'falling back to %r', venue, key, DEFAULT_VENUE)
        venue = DEFAULT_VENUE
    if ui_lang not in ('en', 'zh'):
        ui_lang = 'en'
    return {'kind': kind, 'venue': venue, 'ui_lang': ui_lang}


def make_review_lang(venue: str, ui_lang: str) -> str:
    """Build the composite cache key for a (venue, ui_lang) review."""
    v = (venue or DEFAULT_VENUE).lower()
    if v not in REVIEW_VENUES:
        v = DEFAULT_VENUE
    ul = ui_lang if ui_lang in ('en', 'zh') else 'en'
    return f'{REVIEW_LANG_PREFIX}:{v}:{ul}'


def make_rebuttal_lang(venue: str, ui_lang: str) -> str:
    """Build the sibling composite cache key for a (venue, ui_lang) rebuttal."""
    v = (venue or DEFAULT_VENUE).lower()
    if v not in REVIEW_VENUES:
        v = DEFAULT_VENUE
    ul = ui_lang if ui_lang in ('en', 'zh') else 'en'
    return f'{REBUTTAL_LANG_PREFIX}:{v}:{ul}'


def list_venues() -> list[dict]:
    """Public, frontend-friendly venue list: [{key, name}], registry order."""
    return [{'key': k, 'name': v['name']} for k, v in REVIEW_VENUES.items()]

