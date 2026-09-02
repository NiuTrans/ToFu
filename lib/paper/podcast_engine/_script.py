"""lib/paper/podcast_engine/_script.py — script assembly, LLM call, repair loop.

Turns a persisted paper report into a validated podcast script:

  build messages (report fenced as untrusted + figure list as trusted)
    → dispatch_stream (streamed — intra-call char/segment progress events)
    → parse + normalize
    → validate_script gates
    → ONE validator-feedback revision on failure
    → ONE critic round (semantic fidelity, gated, default ON)
    → ONE critic-feedback revision on failure (re-validated)
    → per-segment est_seconds stamped server-side (the LLM's own estimates
      are never trusted)

A script that still has issues after both revision rounds is returned with
``meta['low_confidence'] = True`` and the remaining issues — the task layer
surfaces this honestly instead of pretending a clean pass.
"""

from __future__ import annotations

import json
import os
import re
import time

from lib.llm_dispatch import dispatch_chat, dispatch_stream
from lib.log import get_logger
from lib.paper.injection_guard import wrap_untrusted
from lib.paper.podcast_prompts import (
    MODE_LENGTH_EN,
    MODE_LENGTH_ZH,
    build_critic_prompt,
    build_script_prompt,
)

from lib.paper.podcast_engine._validate import estimate_seconds, validate_script

logger = get_logger(__name__)

#: Upper bound on script-source characters sent to the model. Reports run
#: 8–25K chars, so 40K is generous headroom; translation-fallback sources are
#: truncated to this (oldest-first keeps the front matter + method intact).
_MAX_SOURCE_CHARS = 40000


class ScriptParseError(Exception):
    """The model's reply could not be coerced into the script JSON shape."""


# ── Figure list (trusted block) ──────────────────────────────────────────


def render_figure_list(images: list[dict]) -> tuple[str, list[str]]:
    """Render the manifest as a prompt block; return (text, allowed_files).

    ``images`` is the figure manifest (``load_image_manifest``): entries
    carry ``url`` (/api/paper/images/<phash>/<file>) + ``caption`` + ``page``.
    The script may only reference the BASENAME via figure_ref.
    """
    files: list[str] = []
    lines: list[str] = []
    for img in images or []:
        url = (img or {}).get('url') or ''
        fname = url.rsplit('/', 1)[-1] if '/' in url else url
        if not fname:
            continue
        files.append(fname)
        caption = ((img or {}).get('caption') or '').strip()
        page = (img or {}).get('page')
        cap_show = caption[:160] if caption else '(无 caption)'
        lines.append(f'- {fname}(第 {page} 页): {cap_show}'
                     if page is not None else f'- {fname}: {cap_show}')
    if not lines:
        return ('(本文没有可用的抽取图片;剧本不得设置 figure_ref,也不得描述任何图)',
                [])
    return ('\n'.join(lines), files)


# ── JSON parse + normalize ───────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r'\{.*\}', re.DOTALL)


def parse_script_json(content: str) -> dict:
    """Coerce the model reply into a script dict; raise ScriptParseError.

    Tolerates markdown fences and leading/trailing chatter by cutting from
    the first ``{`` to the last ``}``.
    """
    text = (content or '').strip()
    if not text:
        raise ScriptParseError('empty reply')
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ScriptParseError('no JSON object found in reply')
    try:
        raw = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ScriptParseError(f'invalid JSON: {e}') from e
    if not isinstance(raw, dict) or not isinstance(raw.get('segments'), list):
        raise ScriptParseError('JSON is not a script object with a segments array')
    return raw


def normalize_script(raw: dict, *, mode: str, lang: str) -> dict:
    """Canonicalize a parsed script: segment ids, defaults, text trimming.

    Drops non-dict segments and empty texts; assigns 0-based ids; defaults
    speaker to 'host' (P1 single voice) and figure_ref to None. The LLM's own
    est_seconds (if any) is discarded — the server stamps its own estimate.
    """
    segs_out: list[dict] = []
    for seg in raw.get('segments') or []:
        if not isinstance(seg, dict):
            continue
        text = (seg.get('text') or '').strip()
        if not text:
            continue
        ref = seg.get('figure_ref')
        segs_out.append({
            'id': len(segs_out),
            'section': (seg.get('section') or '').strip() or 'method',
            'speaker': (seg.get('speaker') or 'host').strip() or 'host',
            'text': text,
            'est_seconds': 0.0,
            'figure_ref': (ref or '').strip() or None,
        })
    return {
        'title': (raw.get('title') or '').strip(),
        'lang': lang,
        'mode': mode,
        'segments': segs_out,
    }


def stamp_estimates(script: dict) -> None:
    """Stamp server-side est_seconds on every segment (never trust the LLM's)."""
    for seg in script.get('segments') or []:
        seg['est_seconds'] = round(estimate_seconds(seg.get('text') or ''), 1)


def script_plain_text(script: dict) -> str:
    """The script rendered as plain paragraphs (critic input / export)."""
    return '\n\n'.join((s or {}).get('text') or ''
                       for s in (script or {}).get('segments') or [])


# ── Critic gate ──────────────────────────────────────────────────────────


def critic_enabled() -> bool:
    """TOFU_PAPER_PODCAST_CRITIC=0 disables the one-round semantic review."""
    return os.environ.get('TOFU_PAPER_PODCAST_CRITIC', '').strip().lower() \
        not in ('0', 'false', 'no', 'off')


def _critic_review(*, lang: str, script: dict, figure_list_text: str,
                   fenced_report: str, model: str | None) -> list[str]:
    """One LLM review round; returns issue strings ([] = pass). Never raises.

    The critic is a SECOND model pass over (script, source) — semantic checks
    the deterministic gates can't do (claim fidelity, caption consistency,
    vague-language detection beyond the watchlists). A critic failure logs a
    warning and passes open: the deterministic gates remain the hard floor.
    """
    prompt = build_critic_prompt(
        lang=lang, script_text=script_plain_text(script),
        figure_list=figure_list_text, report_text=fenced_report)
    try:
        content, _usage = dispatch_chat(
            [{'role': 'user', 'content': prompt}],
            max_tokens=2048, temperature=0, prefer_model=model,
            log_prefix='[Paper:Podcast:Critic]')
    except Exception as e:
        logger.warning('[Paper:Podcast:Critic] review call failed (pass open): %s', e)
        return []
    try:
        m = _JSON_BLOCK_RE.search((content or '').strip())
        payload = json.loads(m.group(0)) if m else {}
        issues = payload.get('issues')
        if isinstance(issues, list):
            return [str(i)[:200] for i in issues if str(i).strip()][:10]
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        logger.warning('[Paper:Podcast:Critic] unparseable verdict (pass open): %s', e)
    return []


# ── Streamed draft (intra-call progress) ─────────────────────────────────

#: Minimum seconds between two streamed `progress` events. The draft is a
#: 1–3 minute call; a beat every 2s is visibly live while keeping the event
#: log the cursor protocol replays to ~90 rows per pass rather than ~180.
_STREAM_EVENT_MIN_INTERVAL = 2.0

#: Counting `"section"` keys counts segments STARTED, which is what the user
#: is watching happen. Tolerates the whitespace the model may put around `:`.
_SECTION_KEY_RE = re.compile(r'"section"\s*:')


def _stream_call(messages: list[dict], *, model: str | None,
                 emit) -> tuple[str, dict]:
    """Run the draft as a STREAM, reporting real progress as bytes arrive.

    ``dispatch_chat`` blocks for the whole 1–3 minute generation, which is
    precisely why the script phase looks frozen. Streaming lets us count
    what has actually been written so far. Both reported numbers are
    MEASURED, never estimated: ``chars`` is the length of the text the model
    has emitted, ``segments`` is the number of ``"section"`` keys in it.

    ``emit(chars, segments)`` is called at most once per
    ``_STREAM_EVENT_MIN_INTERVAL`` seconds.
    """
    buf: list[str] = []
    last_beat = [time.monotonic()]

    def _on_content(delta: str) -> None:
        buf.append(delta)
        now = time.monotonic()
        if now - last_beat[0] < _STREAM_EVENT_MIN_INTERVAL:
            return
        last_beat[0] = now
        text = ''.join(buf)
        emit(len(text), len(_SECTION_KEY_RE.findall(text)))

    def _on_attempt_restart(reason: str = '') -> None:
        # The transport is re-sending this attempt from scratch; anything
        # already counted will arrive again. Keeping it would inflate the
        # char count into a number that never matches the finished script.
        buf.clear()
        emit(0, 0)

    # dispatch_stream returns the assistant MESSAGE dict, not text; the other
    # paper stream callers all accumulate from on_content instead. Doing the
    # same also guarantees the text we parse is byte-identical to the text we
    # counted progress from.
    from lib.llm.stream_result import require_verified_provider_stream_result
    stream_result = require_verified_provider_stream_result(dispatch_stream(
        messages, max_tokens=16384, temperature=0.2, prefer_model=model,
        on_content=_on_content, on_attempt_restart=_on_attempt_restart,
        log_prefix='[Paper:Podcast:Script]'),
        context='paper podcast script')
    return ''.join(buf), stream_result.usage

# ── Main entry ───────────────────────────────────────────────────────────


def generate_script(*, source_text: str, lang: str, mode: str, title: str,
                    images: list[dict], model: str | None,
                    source_kind: str = 'report',
                    on_event=None) -> tuple[dict, dict]:
    """Generate + validate + critic-review a podcast script.

    Args:
        source_text: The material the script is grounded in — normally the
            persisted report markdown; a translation or parsed text as
            fallback (``source_kind`` records which, for the meta).
        lang: Script language ('zh' | 'en').
        mode: 'short' | 'full'.
        title: Paper title for the prompt header.
        images: Figure manifest entries (figure list + figure_ref whitelist).
        model: prefer_model for the dispatch (None = dispatcher default).
        on_event: optional sink for ``progress`` sub-step events
            (docs/modules/ingest_media.md §2.3) — called as
            ``on_event({'type': 'progress', 'phase': 'script', 'unit': 'pass',
            'step': 'draft'|'validate'|'revise'|'critic', ...})`` as the
            pipeline advances. Never let a sink failure break generation.

    Returns:
        (script, meta) — meta carries low_confidence, issues, critic_issues,
        revisions, usage totals and source_kind.
    """
    def _progress(step: str, **extra) -> None:
        if on_event is None:
            return
        try:
            on_event({'type': 'progress', 'phase': 'script', 'unit': 'pass',
                      'step': step, **extra})
        except Exception as e:
            logger.debug('[Paper:Podcast:Script] on_event sink failed: %s', e)
    lang = 'zh' if lang == 'zh' else 'en'
    mode = mode if mode in ('short', 'full') else 'short'
    figure_list_text, manifest_files = render_figure_list(images)
    fenced = wrap_untrusted((source_text or '')[:_MAX_SOURCE_CHARS])

    prompt = build_script_prompt(lang=lang, mode=mode, title=title,
                                 figure_list=figure_list_text,
                                 report_text=fenced)
    base_messages = [{'role': 'user', 'content': prompt}]

    meta: dict = {
        'low_confidence': False, 'issues': [], 'critic_issues': [],
        'revisions': 0, 'source_kind': source_kind,
        'usage': {'input': 0, 'output': 0},
    }

    #: The character target we actually INSTRUCTED the model to hit — a real
    #: number the prompt owns, so `chars/target` is a measured ratio, not a
    #: guessed percentage. English guidance is in WORDS, so the target is
    #: converted to characters at the conventional ~6 chars/word to keep the
    #: two languages on one comparable scale.
    char_target = (MODE_LENGTH_ZH if lang == 'zh' else MODE_LENGTH_EN)[mode][1]
    if lang == 'en':
        char_target *= 6

    def _emit_stream_progress(step: str):
        def _emit(chars: int, segments: int) -> None:
            _progress(step, chars=chars, segments=segments,
                      char_target=char_target)
        return _emit

    def _call(messages: list[dict], *, step: str = 'draft') -> dict:
        content, usage = _stream_call(messages, model=model,
                                      emit=_emit_stream_progress(step))
        if isinstance(usage, dict):
            meta['usage']['input'] += int(usage.get('prompt_tokens') or 0)
            meta['usage']['output'] += int(usage.get('completion_tokens') or 0)
        script = normalize_script(parse_script_json(content), mode=mode, lang=lang)
        if not script['segments']:
            raise ScriptParseError('script has zero non-empty segments')
        return script

    # ── Round 1: initial generation (+1 JSON-repair retry on parse failure) ──
    try:
        script = _call(base_messages)
    except ScriptParseError as e:
        logger.info('[Paper:Podcast:Script] parse failed (%s) — one repair retry', e)
        meta['revisions'] += 1
        _progress('revise', reason='json_repair')
        script = _call(base_messages + [
            {'role': 'assistant', 'content': '(上一条回复不是合法 JSON)'},
            {'role': 'user', 'content':
                '上一条回复不是合法的剧本 JSON。只重新输出 JSON 对象本身,'
                '不要任何解释、不要代码围栏。' if lang == 'zh' else
                'The previous reply was not valid script JSON. Re-output ONLY '
                'the JSON object — no commentary, no fences.'},
        ], step='revise')
    _progress('draft')

    # ── Round 2: validator feedback revision (one shot) ──
    _progress('validate')
    issues = validate_script(script, mode=mode, lang=lang,
                             source_text=source_text or '',
                             manifest_files=manifest_files)
    if issues:
        meta['revisions'] += 1
        _progress('revise', reason='gate_feedback', issues=len(issues))
        issue_list = '\n'.join(f'- {i}' for i in issues)
        logger.info('[Paper:Podcast:Script] %d gate issue(s) — revision round',
                    len(issues))
        script = _call(base_messages + [
            {'role': 'assistant', 'content': json.dumps(script, ensure_ascii=False)},
            {'role': 'user', 'content': (
                '上一版剧本未通过质检,请按以下清单逐条修订后重新输出完整 JSON'
                '(只输出 JSON):\n' + issue_list) if lang == 'zh' else (
                'The previous script failed QA. Fix every issue below and '
                're-output the COMPLETE JSON (JSON only):\n' + issue_list)},
        ], step='revise')
        issues = validate_script(script, mode=mode, lang=lang,
                                 source_text=source_text or '',
                                 manifest_files=manifest_files)
    meta['issues'] = issues

    # ── Round 3: critic semantic review (+1 critic-feedback revision) ──
    if critic_enabled() and not issues:
        _progress('critic')
        critic_issues = _critic_review(lang=lang, script=script,
                                       figure_list_text=figure_list_text,
                                       fenced_report=fenced, model=model)
        if critic_issues:
            meta['revisions'] += 1
            _progress('revise', reason='critic_feedback', issues=len(critic_issues))
            issue_list = '\n'.join(f'- {i}' for i in critic_issues)
            logger.info('[Paper:Podcast:Script] critic raised %d issue(s) — '
                        'revision round', len(critic_issues))
            script = _call(base_messages + [
                {'role': 'assistant', 'content': json.dumps(script, ensure_ascii=False)},
                {'role': 'user', 'content': (
                    '审听编辑提出以下意见,请逐条落实后重新输出完整 JSON'
                    '(只输出 JSON;修订后仍须通过之前的全部质检):\n' + issue_list)
                    if lang == 'zh' else (
                    'The review editor raised the points below. Apply every '
                    'one and re-output the COMPLETE JSON (JSON only; the '
                    'revision must still pass all QA gates):\n' + issue_list)},
            ], step='revise')
            # A critic revision must STILL pass the hard gates — re-validate.
            issues = validate_script(script, mode=mode, lang=lang,
                                     source_text=source_text or '',
                                     manifest_files=manifest_files)
            meta['issues'] = issues
            meta['critic_issues'] = critic_issues

    stamp_estimates(script)
    if meta['issues']:
        meta['low_confidence'] = True
        logger.warning('[Paper:Podcast:Script] script accepted WITH %d remaining '
                       'gate issue(s) after %d revision(s) — low_confidence',
                       len(meta['issues']), meta['revisions'])
    else:
        logger.info('[Paper:Podcast:Script] script clean — %d segments, '
                    '%d revision(s), lang=%s mode=%s',
                    len(script.get('segments') or []), meta['revisions'], lang, mode)
    return script, meta


__all__ = [
    'ScriptParseError',
    'render_figure_list',
    'parse_script_json',
    'normalize_script',
    'stamp_estimates',
    'script_plain_text',
    'critic_enabled',
    'generate_script',
]
