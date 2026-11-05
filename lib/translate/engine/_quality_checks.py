"""Pure translation content checks and leading-reasoning cleanup.

Entry points: ordered verdict predicates consumed by the candidate assessor.
Dependencies: sentence completeness and shared language classification; no
dispatch, retries, cache mutation or lifecycle ownership lives here.
"""
import re

from ._split import _ends_midsentence

# MiniMax and similar cheap translators wrap *their own* reasoning in
# <think>...</think>. Source documents also *mention* those tags (API
# tables, inline code). Slicing from the first unclosed <think> to EOF
# deleted everything after a documented mention.
_THINK_OPEN_RE = re.compile(r'<think>', re.IGNORECASE)
_LEADING_THINK_BLOCK_RE = re.compile(
    r'\A\s*<think>[\s\S]*?</think>\s*',
    re.IGNORECASE,
)
_LEADING_UNCLOSED_THINK_RE = re.compile(r'\A\s*<think>', re.IGNORECASE)


def _strip_model_thinking(text):
    """Drop leading model-reasoning wrappers; keep later literal tags."""
    if not text or not _THINK_OPEN_RE.search(text):
        return text
    out = text
    while True:
        match = _LEADING_THINK_BLOCK_RE.match(out)
        if not match:
            break
        out = out[match.end():]
    if _LEADING_UNCLOSED_THINK_RE.match(out):
        # Incomplete model thinking at the start of the completion.
        # A mid-document <think> mention must not take this branch.
        return ''
    return out


def _translation_truncation_verdict(text, clen, finish, target_is_chinese,
                                    model):
    """Return ``(is_truncated, reason)`` for one non-empty candidate."""
    is_truncated = False
    reason = ''

    # EN→ZH output is naturally ~0.4-0.7x the source length, so a flat
    # 20% floor lets a translation that silently dropped HALF the
    # document (the dominant 漏译 mode — cheap models stop early with
    # finish_reason=stop) sail through undetected. Raise the floor for
    # Chinese targets so those drops are caught and retried.
    short_floor = 0.30 if target_is_chinese else 0.20
    # A SOFT floor (above the hard floor) that only bites when the output
    # ALSO ended mid-sentence — this catches the finish_reason=stop early
    # stop that lands between the hard floor and a full translation (the
    # exact bug: a 40%-length body ending on a bare word like "…are you"
    # cleared the 0.30 hard floor and was accepted). Ratio alone can't
    # raise the hard floor (a legit terse EN→ZH is ~0.35-0.5), so we
    # require the mid-sentence signal to avoid false positives.
    soft_trunc_floor = 0.45 if target_is_chinese else 0.35
    if finish == 'length':
        is_truncated = True
        reason = f'finish_reason=length, model={model}'
    elif clen > 500 and len(text) < clen * short_floor:
        is_truncated = True
        reason = (f'output too short ({len(text)}/{clen} = {len(text)/clen*100:.0f}%, '
                  f'floor={short_floor:.0%}), model={model}')
    elif (clen > 200 and len(text) < clen * soft_trunc_floor
          and _ends_midsentence(text)):
        is_truncated = True
        reason = (f'ends mid-sentence + short ({len(text)}/{clen} = '
                  f'{len(text)/clen*100:.0f}%, soft floor={soft_trunc_floor:.0%}, '
                  f"tail={text.rstrip()[-16:]!r}), model={model}")
    return is_truncated, reason


def _targets_chinese(target):
    """Whether the translation target names Chinese (``Chinese`` or ``zh*``)."""
    target = (target or '').lower()
    return target.startswith('chinese') or 'zh' in target


def _translation_noop_verdict(output, source_text, target, model):
    """Return ``(is_noop, reason)`` when stripped output echoes the source."""
    from lib.text_lang import cjk_ratio, is_predominantly_chinese

    is_noop = False
    noop_reason = ''
    # When the source is ALREADY in the target language, verbatim output
    # is a legitimate pass-through, not an echo.
    target_is_chinese = _targets_chinese(target)
    src_already_target = target_is_chinese and is_predominantly_chinese(source_text)
    if (not src_already_target
            and output == source_text and len(source_text) >= 40):
        is_noop = True
        noop_reason = f'output==input verbatim ({len(output)} chars), model={model}'
    elif target_is_chinese:
        # Translating TO Chinese — output must contain a meaningful CJK ratio.
        # Skip the check on tiny chunks (likely all code/URLs) and on inputs
        # that are already Chinese (rule 7 scenario the prompt no longer covers).
        if len(output) >= 200 and cjk_ratio(source_text) < 0.1:
            out_cjk = cjk_ratio(output)
            if out_cjk < 0.05:
                is_noop = True
                noop_reason = (f'output cjk_ratio={out_cjk:.2%} for target=Chinese '
                               f'(len={len(output)}), model={model}')
    return is_noop, noop_reason


def _translation_flip_verdict(output, source_text, target_is_chinese, model):
    """Return ``(is_flip, reason)`` when output rewrote Chinese into English."""
    from lib.text_lang import cjk_ratio, is_predominantly_english

    is_flip = False
    flip_reason = ''
    src_cjk = cjk_ratio(source_text)
    out_cjk = cjk_ratio(output)
    # DIRECTIONAL verdict (owner adjudication 2026-07-27): a true flip
    # always moves the CJK share DOWN toward zero. Output that keeps or
    # raises the CJK share while staying latin-dominant is a FAITHFUL
    # translation of code/identifier-dense text — the model translated
    # the prose, the identifiers correctly stayed Latin. The old
    # latin-dominance-only check could never pass such chunks and burned
    # the full roster on them (production: 252 refusals/day, one chunk
    # ×36). Only judge a flip when the CJK share actually fell.
    if (target_is_chinese and len(output) >= 200
            and src_cjk >= 0.10
            and is_predominantly_english(output)
            and out_cjk < src_cjk):
        is_flip = True
        flip_reason = (
            f'wrong-language flip: target=Chinese, source cjk_ratio='
            f'{src_cjk:.2f} but output latin-dominant '
            f'(out cjk={out_cjk:.2f}, len={len(output)}), '
            f'model={model}')
    return is_flip, flip_reason


def overgeneration_verdict(output, clen, model, *, ratio, absolute, minimum_input):
    """Return ``(is_overgen, reason)`` for output far longer than its input."""
    is_overgen = False
    overgen_reason = ''
    if (clen >= minimum_input
            and len(output) > clen * ratio
            and len(output) - clen > absolute):
        is_overgen = True
        overgen_reason = (
            f'over-generated: output {len(output)}/{clen} = '
            f'{len(output)/clen:.1f}x input (ratio floor={ratio:.0f}x, '
            f'abs floor={absolute}), model={model}')
    return is_overgen, overgen_reason


def translation_candidate_verdict(text, source_text, *, clen, target,
                                  target_is_chinese, finish, model,
                                  overgeneration_verdict):
    """Classify non-empty text in policy order; stop at the first rejection."""
    rejected, reason = _translation_truncation_verdict(
        text, clen, finish, target_is_chinese, model)
    if rejected:
        return 'truncated', reason
    rejected, reason = _translation_noop_verdict(text, source_text, target, model)
    if rejected:
        return 'noop', reason
    rejected, reason = _translation_flip_verdict(
        text, source_text, target_is_chinese, model)
    if rejected:
        return 'wrong_language', reason
    rejected, reason = overgeneration_verdict(text, clen, model)
    if rejected:
        return 'over_generated', reason
    return 'ok', ''
