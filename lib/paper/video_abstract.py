"""lib/paper/video_abstract.py — Paper → narrated video abstract (P3).

The motion-video pipeline's paper entry point, sibling to the podcast
chain (report → spoken script → TTS audio): here the report becomes a
short narrated MG video (report → scene beats → motion engine).

Flow:

  1. ``has_report`` gate (same report-first UX as the podcast start route);
  2. source text via the podcast chain's ``_load_source_text`` (report in
     the requested language → other language → translation → parsed text);
  3. :func:`build_abstract_scenes` — beats carrying THREE separate fields
     (see below), each one sized to fit both its frame and its time slot;
  4. the scenes drive the motion engine in scenes-only mode (no SRT —
     loose alignment lets TTS narration stretch each beat to fit);
  5. progress/results ride the motion runtime (poll/file endpoints).

**The three-field scene contract** (owner 2026-07-27). The original
implementation put one paragraph of report prose into ``text`` and let it
serve as narration, on-screen headline AND subtitle at once. That is what
produced 1898-char headlines at 46px on a 1440px frame. A beat now carries:

  * ``text``      — SPOKEN narration (TTS + sidecar SRT), budgeted so it is
                    utterable inside the scene's own duration;
  * ``on_screen`` — the ON-FRAME caption, bounded by the template's measured
                    :func:`~lib.motion_video._template.on_screen_capacity`;
  * ``visual``    — ART DIRECTION for the per-scene author. Untouched
                    semantics: it is NOT drawn as the headline.

**Beats are written, not sliced.** The default path rewrites the report with
an LLM through the SAME script stage the topic recipe uses
(:func:`lib.motion_video._recipe.script_stage_for_source`), so there is one
implementation of "prose → spoken beats" in the tree. Slicing prose on a
character budget is kept ONLY as the zero-LLM fallback
(:func:`slice_abstract_beats`) for when no model is reachable, and its output
must satisfy the same budget as the LLM path.

**No silent clamping.** When a beat's text does not fit its slot the builder
RE-CUTS (splits the beat, or trims the caption) — it never parks the scene on
``_MAX_SCENE_S`` and calls it done. A duration sitting exactly on the ceiling
is a swallowed error, and :func:`lib.motion_video.check_scene_budget` fails
the storyboard for it.
"""

from __future__ import annotations

import json
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_abstract_scenes', 'slice_abstract_beats',
           'start_video_abstract']

#: Roughly 250 chars/min narration pace (podcast chain's estimate scale).
_CHARS_PER_SECOND = 4.2
_MIN_SCENE_S = 3.0
#: Ceiling for a single beat. A scene is never PARKED here — reaching it means
#: the beat must be split (the budget gate treats saturation as an error).
_MAX_SCENE_S = 15.0
_DEFAULT_MAX_SCENES = 8

#: Spoken chars that fit one beat without saturating it. Leaves a margin below
#: the ceiling so rounding can never land a scene exactly on it.
_BEAT_CHAR_BUDGET = int((_MAX_SCENE_S - 1.0) * _CHARS_PER_SECOND)

#: How far a beat may exceed the budget before it is SPLIT rather than absorbed.
#: Splitting costs the authored caption and art direction of the tail piece, so
#: a modest overrun is better absorbed. The factor is bounded by the ceiling:
#: ``_BEAT_CHAR_BUDGET * 1.15 / _CHARS_PER_SECOND`` ≈ 15.3s would exceed 15.0s,
#: so it is capped at whatever still lands strictly under ``_MAX_SCENE_S``.
_OVERRUN_TOLERANCE = min(
    1.15, (_MAX_SCENE_S - 0.5) * _CHARS_PER_SECOND / _BEAT_CHAR_BUDGET)

_MD_NOISE_RE = re.compile(
    r'^\s{0,3}#{1,6}\s*|^\s{0,3}[-*+]\s+|^\s{0,3}>\s?|\*\*|__|`{1,3}|'
    r'\[([^\]]+)\]\([^)]*\)|<[^>]+>',
    re.M)

_SENTENCE_SPLIT_RE = re.compile(r'(?<=[。！？!?…])\s*|(?<=[.!?])\s+')


def _clean_markdown(text: str) -> str:
    """Strip markdown structure, keeping readable prose."""
    text = _MD_NOISE_RE.sub(lambda m: m.group(1) or ' ', text)
    lines = [ln.strip() for ln in text.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text or '') if s.strip()]


#: Characters we may break a hard-split on, preferred over a mid-token cut.
#: CJK text has no spaces, so punctuation and ASCII whitespace are the only
#: honest boundaries available without a tokenizer.
_BREAK_CHARS = ' \t，,、；;：:）)】」』—–/·'


def _hard_split(sent: str, budget: int) -> tuple[str, str]:
    """Cut ``sent`` at or before ``budget``, preferring a token boundary.

    Falls back to an exact cut only when no boundary exists in the last third
    of the window — cutting 'Discriminative' into 'Di' + 'scriminative' is how
    a caption turns into noise, so we look for somewhere legitimate first.
    """
    window = sent[:budget]
    floor = max(1, int(budget * 0.6))
    best = -1
    for ch in _BREAK_CHARS:
        idx = window.rfind(ch)
        if idx >= floor:
            best = max(best, idx)
    if best < 0:
        return window.rstrip(), sent[budget:].lstrip()
    # +1 so the boundary char stays with the head when it is punctuation.
    cut = best if window[best] in ' \t' else best + 1
    return sent[:cut].rstrip(), sent[cut:].lstrip()


def _split_to_budget(spoken: str, budget: int) -> list[str]:
    """Split one over-long beat into pieces that each fit ``budget`` chars.

    Prefers sentence boundaries; a single sentence longer than the budget is
    broken at a token boundary (never mid-word), because leaving it whole is
    what the clamp used to do.
    """
    if len(spoken) <= budget:
        return [spoken]
    pieces: list[str] = []
    cur = ''
    for sent in _sentences(spoken) or [spoken]:
        while len(sent) > budget:
            head, sent = _hard_split(sent, budget)
            if not head:
                break
            if cur:
                pieces.append(cur)
                cur = ''
            pieces.append(head)
        if not sent:
            continue
        if not cur:
            cur = sent
        elif len(cur) + 1 + len(sent) <= budget:
            cur = f'{cur} {sent}'
        else:
            pieces.append(cur)
            cur = sent
    if cur:
        pieces.append(cur)
    return pieces or [spoken[:budget]]


#: Clause separators used to break a beat into candidate caption units.
_CLAUSE_SPLIT_RE = re.compile(r'[。！？!?…；;，,、:：\n]+')


def _salient_terms(text: str) -> dict:
    """Character-bigram frequencies over the WHOLE document.

    A deliberately simple, deterministic salience signal: CJK has no spaces,
    so word frequency needs a tokenizer, but character bigrams approximate
    term frequency well enough to rank clauses. Computed over the whole report
    (not the beat) so a clause is scored by how central it is to the PAPER.
    """
    clean = re.sub(r'[\s\W_]+', '', text or '')
    freq: dict = {}
    for i in range(len(clean) - 1):
        bg = clean[i:i + 2]
        freq[bg] = freq.get(bg, 0) + 1
    return freq


#: A caption must be a real condensation of its beat, not a near-copy.
_CAPTION_MAX_SHARE = 0.75


def _split_balanced(spoken: str, budget: int) -> list[str]:
    """Split an over-long beat into NEARLY EQUAL pieces, never a runt tail.

    ``_split_to_budget`` fills each piece to the budget, so a 67-char beat at
    a 58-char budget yields 58 + 9 — and a 9-char scene is floored to
    ``_MIN_SCENE_S``, i.e. three seconds of screen time carrying nothing.
    Choosing the piece COUNT first and dividing evenly keeps every piece
    substantial (67 -> 34 + 33).
    """
    if len(spoken) <= budget:
        return [spoken]
    import math
    n = math.ceil(len(spoken) / budget)
    target = math.ceil(len(spoken) / n)
    pieces: list[str] = []
    rest = spoken
    while rest and len(pieces) < n - 1:
        head, rest = _hard_split(rest, target)
        if not head:
            break
        pieces.append(head)
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p] or [spoken[:budget]]


def _compress(spoken: str, capacity: int, terms: dict,
              *, index: int = 0, total: int = 0, lang: str = 'zh') -> str:
    """The on-frame caption for a beat WITHOUT a model to write one.

    **Why this does not extract a clause from the narration.** Measured on a
    real 16.7k-char report: picking the most salient clause of each beat
    produced grammatical debris in 6 of 8 scenes — ``qualitative/``,
    ``prompted 39.7%）——``, ``2026 的 backward inference（small`` — because a
    clause of running prose is not a headline, and no threshold turns it into
    one. Tightening the length bounds only changes the SHAPE of the debris.

    So the fallback stops pretending: it emits a **structural label** (beat
    position) which is short, always grammatical, and honestly signals "no
    model wrote this". The narration still carries the content, and the
    real captions come from :func:`_llm_beats`. This is the floor that keeps
    a model-less host renderable, not the intended output.
    """
    if total and index:
        if lang == 'en':
            return f'Key point {index} of {total}'
        return f'要点 {index} / {total}'
    return ('Key point' if lang == 'en' else '要点')


def _timeline(beats: list[dict], *, chars_per_second: float,
              min_scene_s: float, max_scene_s: float) -> list[dict]:
    """Lay budgeted beats onto a contiguous timeline from 0.0.

    Duration comes from the beat's spoken length. Because every beat was
    already split to the char budget, no duration can reach ``max_scene_s`` —
    so there is nothing to clamp. The assertion is the guard: if a beat still
    over-runs we would rather fail loudly here than ship a clamped scene.
    """
    scenes: list[dict] = []
    cursor = 0.0
    for i, beat in enumerate(beats, 1):
        spoken = beat['text']
        dur = max(min_scene_s, len(spoken) / chars_per_second)
        if dur >= max_scene_s:
            raise ValueError(
                f'beat {i} needs {dur:.1f}s (> {max_scene_s}s ceiling) after '
                'budgeting — the split step failed to bound it')
        dur = round(dur, 3)
        scenes.append({
            'id': f'scene-{i:03d}',
            'start': round(cursor, 3),
            'end': round(cursor + dur, 3),
            'text': spoken,
            'on_screen': beat['on_screen'],
            'visual': beat.get('visual', ''),
            'assets': beat.get('assets') or [],
        })
        cursor = round(cursor + dur, 3)
    return scenes


def slice_abstract_beats(source_text: str, *,
                         max_scenes: int = _DEFAULT_MAX_SCENES,
                         capacity: int = 0,
                         lang: str = 'zh') -> list[dict]:
    """Zero-LLM fallback: one extractive beat per document SEGMENT.

    Returns beat dicts (``text`` / ``on_screen`` / ``visual``), NOT scenes —
    :func:`_timeline` turns them into a storyboard.

    **Why segments and not the leading paragraphs.** A ``max_scenes``-beat film
    can hold only ``max_scenes * _BEAT_CHAR_BUDGET`` spoken chars — 464 at the
    defaults, against a ~16.7k-char report. Reading from the top until the
    budget runs out therefore ships the first ~3% of the paper and silently
    discards the rest, so the abstract never reaches the results or the
    limitations. Instead the report is split into ``max_scenes`` equal
    segments and each contributes its most salient sentence: the film samples
    the WHOLE document (structure preserved, since segments stay in order)
    rather than truncating it. Fidelity per beat is lower than an LLM rewrite
    — that is the honest cost of having no model, and why this is the fallback.
    """
    from lib.motion_video._template import CAPTION_FONT_PX, on_screen_capacity

    capacity = capacity or on_screen_capacity(font_px=CAPTION_FONT_PX)
    clean = _clean_markdown(source_text or '')
    sents = _sentences(clean)
    if not sents:
        return []

    terms = _salient_terms(clean)
    max_scenes = max(1, int(max_scenes))
    # Contiguous segments covering every sentence; the last absorbs the
    # remainder so no tail is dropped by integer division.
    per = max(1, len(sents) // max_scenes)
    segments: list[list[str]] = []
    for i in range(max_scenes):
        lo = i * per
        if lo >= len(sents):
            break
        hi = len(sents) if i == max_scenes - 1 else min(len(sents), lo + per)
        segments.append(sents[lo:hi])

    def salience(sent: str) -> float:
        body = re.sub(r'[\s\W_]+', '', sent)
        if len(body) < 2:
            return 0.0
        total = sum(terms.get(body[i:i + 2], 0) for i in range(len(body) - 1))
        return total / (len(body) - 1)

    beats: list[dict] = []
    for seg in segments:
        if not seg:
            continue
        lead = max(seg, key=salience)
        spoken = _split_to_budget(lead, _BEAT_CHAR_BUDGET)[0]
        beats.append({'text': spoken, 'on_screen': '', 'visual': ''})
    total = len(beats)
    for i, beat in enumerate(beats, 1):
        beat['on_screen'] = _compress(beat['text'], capacity, terms,
                                      index=i, total=total, lang=lang)
    return beats


def _llm_beats(source_text: str, *, lang: str, max_scenes: int,
               capacity: int, model: str | None = None) -> list[dict]:
    """Rewrite the report into spoken beats + captions via the script stage.

    Reuses :func:`lib.motion_video._recipe.script_stage_for_source` so the
    tree has ONE implementation of "prose → spoken beats + on-screen captions
    + art direction". Returns [] when the model is unreachable or its output
    fails the shape check, letting the caller fall back to slicing.
    """
    try:
        from lib.motion_video._recipe import script_stage_for_source
        beats = script_stage_for_source(
            source_text, lang=lang, max_scenes=max_scenes,
            char_budget=_BEAT_CHAR_BUDGET, caption_capacity=capacity,
            model=model)
    except Exception as e:
        logger.warning('[Paper:Video] LLM beat rewrite failed (%s) — falling '
                       'back to zero-LLM slicing', e)
        return []
    return beats or []


def build_abstract_scenes(source_text: str, *,
                          max_scenes: int = _DEFAULT_MAX_SCENES,
                          min_scene_s: float = _MIN_SCENE_S,
                          max_scene_s: float = _MAX_SCENE_S,
                          chars_per_second: float = _CHARS_PER_SECOND,
                          lang: str = 'zh',
                          use_llm: bool = True,
                          model: str | None = None) -> list[dict]:
    """Build a budget-satisfying storyboard from a paper report.

    Beats are written by the shared script stage when a model is reachable and
    sliced deterministically otherwise; either way every beat is bounded to
    what it can SAY in its slot and what its caption can SHOW on the frame,
    then laid out contiguously from 0.0.

    Returns motion-engine scene dicts with ``text`` / ``on_screen`` /
    ``visual``, valid against both :func:`lib.motion_video.check_storyboard`
    and :func:`lib.motion_video.check_scene_budget`.
    """
    from lib.motion_video._template import CAPTION_FONT_PX, on_screen_capacity

    # Budgeted at the CAPTION size, not the 46px floor: a caption that only
    # fits at the floor is a paragraph on a title card.
    capacity = on_screen_capacity(font_px=CAPTION_FONT_PX)
    terms = _salient_terms(_clean_markdown(source_text or ''))
    beats: list[dict] = []
    if use_llm:
        beats = _llm_beats(source_text, lang=lang, max_scenes=max_scenes,
                           capacity=capacity, model=model)
    if not beats:
        beats = slice_abstract_beats(source_text, max_scenes=max_scenes,
                                     capacity=capacity, lang=lang)
    if not beats:
        return []

    # Enforce the budget on WHATEVER produced the beats — the LLM is asked for
    # bounded output but never trusted to have obeyed.
    #
    # Splitting is a LAST resort, not the normal path: a beat cut in two leaves
    # the second half with no authored caption (it would fall back to a
    # positional placeholder) and duplicates the first half's art direction.
    # Measured on a real run, splitting at exactly the budget degraded 3 of 8
    # scenes that way. A modest overrun is therefore ABSORBED — the beat keeps
    # its authored caption and simply occupies a slightly longer slot, which is
    # still bounded well under the saturation ceiling.
    absorb = int(_BEAT_CHAR_BUDGET * _OVERRUN_TOLERANCE)
    bounded: list[dict] = []
    for beat in beats:
        spoken = str(beat.get('text') or '').strip()
        if not spoken:
            continue
        caption = str(beat.get('on_screen') or '').strip()
        visual = str(beat.get('visual') or '').strip()
        assets = beat.get('assets') or []
        pieces = ([spoken] if len(spoken) <= absorb
                  else _split_balanced(spoken, _BEAT_CHAR_BUDGET))
        # A split beat is ONE beat continuing across two scenes, so its
        # authored caption and art direction apply to every piece. Blanking
        # them for the tail (an earlier attempt) produced placeholder captions
        # on 3 of 8 scenes in a real run — the split is our doing, not a
        # change of subject.
        usable = (caption and len(caption) <= capacity
                  and caption not in pieces)
        for piece in pieces:
            cap = caption if usable else _compress(
                piece, capacity, terms, index=len(bounded) + 1,
                total=max_scenes, lang=lang)
            bounded.append({'text': piece, 'on_screen': cap,
                            'visual': visual, 'assets': assets})
        if len(bounded) >= max_scenes:
            break
    bounded = bounded[:max_scenes]
    if not bounded:
        return []

    scenes = _timeline(bounded, chars_per_second=chars_per_second,
                       min_scene_s=min_scene_s, max_scene_s=max_scene_s)
    logger.info('[Paper:Video] abstract scenes: %d beat(s), %.1fs total '
                '(source=%s)', len(scenes), scenes[-1]['end'] if scenes else 0,
                'llm' if use_llm and beats else 'sliced')
    return scenes


def start_video_abstract(paper_hash: str, *, lang: str = 'zh',
                         voice: str = '', speed=None,
                         alignment: str = 'loose', narration: bool = True,
                         burn_in: bool = False, quality: str = 'standard',
                         parallel: int = 2, max_scenes: int = _DEFAULT_MAX_SCENES,
                         scene_author: bool | None = None,
                         model: str | None = None,
                         force: bool = False) -> dict:
    """Start a motion-engine task rendering this paper's video abstract.

    Dedup (§2.1 of docs/PAPER_MEDIA_UX_DESIGN.md): a second call with the
    same (paper_hash, lang, voice, narration, burn_in, quality) joins the
    in-flight task instead of starting a parallel render — same contract
    as the motion main route. ``force=True`` (the frontend's Regenerate
    button) explicitly bypasses the index, like the podcast's ``force``.

    Returns ``{'ok', 'task_id', 'scenes', 'source_kind'}`` (plus
    ``deduped: True`` on a join) or
    ``{'ok': False, 'reason': 'report_required'|'empty_source'|'budget_failed'}``.
    """
    from lib import motion_video as mv
    from lib.motion_video._env import motion_root
    from lib.motion_video.engine import run_motion_task
    from lib.motion_video.runtime import (
        _motion_index_get,
        _motion_index_register,
        _motion_runtime,
        _motion_task_id,
        _new_motion_task,
    )
    from lib.paper.podcast_engine import _load_source_text, has_report

    if not has_report(paper_hash):
        return {'ok': False, 'reason': 'report_required'}

    # The model is part of the dedup identity: joining an in-flight render
    # made with a DIFFERENT model would silently serve the wrong film — the
    # cache-key-skew family (same class as the podcast cache row).
    dedup_key = ('paper', paper_hash, lang, voice, bool(narration),
                 bool(burn_in), quality, scene_author, model or '')
    if not force:
        existing = _motion_index_get(dedup_key)
        if existing:
            logger.info('[Paper:Video] dedup join: %s (paper=%s)',
                        existing, paper_hash[:8])
            return {'ok': True, 'task_id': existing, 'deduped': True,
                    'scenes': 0, 'source_kind': 'joined'}

    text, kind = _load_source_text(paper_hash, lang)
    if not text.strip():
        return {'ok': False, 'reason': 'empty_source'}
    scenes = build_abstract_scenes(text, max_scenes=max_scenes, lang=lang,
                                   model=model)
    if not scenes:
        return {'ok': False, 'reason': 'empty_source'}

    # Fail BEFORE spending a render: the engine would otherwise compose and
    # burn ~35s/scene on a storyboard we already know cannot read.
    budget_errors = mv.check_scene_budget(scenes, width=1080, height=1440,
                                          max_scene_s=_MAX_SCENE_S,
                                          narration=narration)
    if budget_errors:
        logger.error('[Paper:Video] storyboard failed the scene budget for '
                     'paper=%s: %s', paper_hash[:8], '; '.join(budget_errors[:3]))
        return {'ok': False, 'reason': 'budget_failed',
                'errors': budget_errors[:6]}

    task_id = _motion_task_id()
    _motion_index_register(dedup_key, task_id)
    workdir = os.path.join(motion_root(), 'jobs', task_id)
    os.makedirs(workdir, exist_ok=True)
    scenes_path = os.path.join(workdir, 'scenes.json')
    with open(scenes_path, 'w', encoding='utf-8') as f:
        json.dump(scenes, f, ensure_ascii=False, indent=1)

    task = _new_motion_task(
        task_id, srt_path='', workdir=workdir, voice=voice, speed=speed,
        alignment=alignment, narration=narration, quality=quality,
        parallel=parallel, width=1080, height=1440,
        scenes_path=scenes_path)
    task['burn_in'] = burn_in
    task['burn_in_fontsdir'] = ''
    task['paper_hash'] = paper_hash
    # The composing model, threaded to the scene author (engine compose
    # stage) and persisted in the manifest so a crash-resume does not
    # silently swap the user's pick for the dispatcher default.
    task['model'] = model or ''
    # Written ONLY when the caller stated a preference. Leaving the key absent
    # is what lets scene_author_enabled() own the default — hard-coding True
    # here would re-create the per-caller copy that made this panel ship
    # template cards for a whole release after the tool path was fixed.
    if scene_author is not None:
        task['scene_author'] = bool(scene_author)
    _motion_runtime.spawn(task_id, run_motion_task, task)
    logger.info('[Paper:Video] abstract started: %s (paper=%s scenes=%d '
                'narration=%s)', task_id, paper_hash[:8], len(scenes),
                narration)
    return {'ok': True, 'task_id': task_id, 'scenes': len(scenes),
            'source_kind': kind}
