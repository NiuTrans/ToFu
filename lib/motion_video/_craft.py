"""lib/motion_video/_craft.py — the deep craft corpus, reachable from the author.

**The dead instruction** ( item ③, measured 2026-07-29).
``guide/WORKFLOW.md`` told the author *"Activate `hyperframes-motion` when a
scene needs real choreography"* — but ``load_skill`` is a CHAT-agent tool,
and the headless per-scene author's toolset is a fixed five
(``write_composition`` / ``composition_check`` / ``web_search`` /
``generate_asset`` / ``fetch_url``). Measured: skill/blueprint hits in
``_scene_author.py`` = **0**. So the sentence pointed at a capability the
engine path could not reach, and every film was authored from the ~20 KB
distilled guide alone while 29 atomic motion rules and 13 multi-phase
blueprints sat in a catalog entry nobody had installed.

**Why this is a managed asset, not a Settings → Skills install.** The skills
catalog is a USER channel: install is a human action, and
``lib.skills.registry.list_skills()`` measured **[]** on this host. A tool that
reads *installed* packages would therefore be dead on exactly the machine that
needs it. The motion pipeline already self-provisions everything it depends on
— the pinned HyperFrames CLI (:func:`lib.motion_video._env.ensure_hyperframes`)
and the CJK sans face (:func:`lib.motion_video._fonts.ensure_cjk_sans`) — so
the craft corpus rides the SAME contract: fetched once into the managed motion
root, then read locally forever. The URL is not re-typed here; it is imported
from the catalog entry so the two can never drift.

**Progressive disclosure is the whole design.** The corpus is 104 KB across
104 files. Injecting it would blow the per-scene budget and drown the beat's
own art direction. Instead the author gets two narrow reads:

  * ``craft_index()`` — the rules/blueprints INDEX (name + one-line summary +
    tags), ~6 KB, cheap enough to hand over with the prompt;
  * ``craft_reference(name)`` — ONE rule or blueprint in full, on demand.

That mirrors how the chat agent uses ``load_skill`` (index every turn →
full text on match) without borrowing a tool the engine cannot call.

Never fatal: an unreachable network leaves the corpus absent and every
function degrades to empty, so a film still authors from the in-tree guide.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['craft_root', 'ensure_craft_corpus', 'craft_index',
           'craft_reference', 'craft_available', 'CRAFT_PACKS']

#: The packs worth shipping to a scene author, and what each is FOR.
#:
#: ``motion-graphics`` and the CLI/registry packs are deliberately excluded:
#: the first is a sub-agent pipeline paradigm (we have our own chassis), the
#: others document a CLI the author cannot invoke. Only knowledge that changes
#: what a COMPOSITION looks like earns its context budget.
CRAFT_PACKS = ('hyperframes-motion', 'hyperframes-design')

#: Index files inside a pack, in the order an author should meet them. ALL of
#: them are read, not just the first: ``rules-index.md`` advertises the 29
#: atomic techniques and ``blueprints-index.md`` the 13 multi-phase scene
#: templates, so stopping at the first match would silently starve the author
#: of every blueprint (measured: 12.6 KB never read).
_INDEX_FILES = ('rules-index.md', 'blueprints-index.md')

#: A single reference must never blow the per-scene budget on its own.
_MAX_REFERENCE_BYTES = 24_000
#: The combined index handed to the author with its prompt. Sized from the
#: real corpus — measured 11.0 KB of rules + 12.7 KB of blueprints + a
#: synthesised design listing — so the full catalogue arrives untruncated.
#: A truncated index is not a smaller index, it is a silently unreachable
#: tail, which is the defect this module exists to remove.
_MAX_INDEX_BYTES = 40_000


def craft_root() -> str:
    """Directory holding the managed craft corpus (under the motion root)."""
    from lib.motion_video._env import motion_root
    path = os.path.join(motion_root(), 'craft')
    os.makedirs(path, exist_ok=True)
    return path


def _pack_dir(pack: str) -> str:
    return os.path.join(craft_root(), pack)


def craft_available() -> bool:
    """True when at least one pack is already on disk (no network touched)."""
    return any(os.path.isdir(_pack_dir(p)) for p in CRAFT_PACKS)


def ensure_craft_corpus(*, download: bool = True, timeout: int = 180) -> bool:
    """Materialise the craft packs into the managed root. Returns availability.

    Fetched ONCE from the same archive the skills catalog points at — imported
    from :mod:`lib.skills.catalog` rather than re-typed, so a URL change cannot
    leave two copies disagreeing.

    Never raises: an unreachable network logs and returns False, and the author
    simply keeps working from the in-tree guide.
    """
    if craft_available():
        return True
    if not download:
        return False

    try:
        from lib.skills.catalog import (_HYPERFRAMES_SKILLS_PREFIX,
                                        _HYPERFRAMES_ZIP)
    except Exception as e:
        logger.warning('[Craft] cannot read the catalog constants: %s', e)
        return False

    import io
    import zipfile

    from lib.http_client import http_get
    try:
        resp = http_get(_HYPERFRAMES_ZIP, timeout=timeout)
        data = getattr(resp, 'content', b'') or b''
        code = getattr(resp, 'status_code', 0)
    except Exception as e:
        logger.warning('[Craft] fetch failed (%s) — the author will work from '
                       'the in-tree guide only', e)
        return False
    if code != 200 or len(data) < 10_000:
        logger.warning('[Craft] rejected the archive (HTTP %s, %d bytes)',
                       code, len(data))
        return False

    wanted = tuple(f'{_HYPERFRAMES_SKILLS_PREFIX}/{p}/' for p in CRAFT_PACKS)
    written = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Strip the archive's top-level "<repo>-<ref>/" component.
                rel = info.filename.split('/', 1)[-1]
                match = next((w for w in wanted if rel.startswith(w)), '')
                if not match:
                    continue
                # Only text knowledge — a pack also carries runnable examples
                # and image assets the author cannot use and must not pay for.
                if os.path.splitext(rel)[1].lower() not in ('.md', '.html'):
                    continue
                pack = match.rsplit('/', 2)[-2]
                tail = rel[len(match):]
                dest = os.path.join(_pack_dir(pack), tail)
                # Never let an archive path escape its pack directory.
                root = os.path.realpath(_pack_dir(pack))
                if not os.path.realpath(dest).startswith(root + os.sep):
                    logger.warning('[Craft] refusing escaping path %r', rel)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(info) as src, open(dest, 'wb') as out:
                    out.write(src.read())
                written += 1
    except Exception as e:
        logger.warning('[Craft] archive extraction failed: %s', e, exc_info=True)
        return craft_available()

    logger.info('[Craft] corpus ready: %d file(s) across %s', written,
                ', '.join(CRAFT_PACKS))
    return craft_available()


def _drop_dead_entries(body: str, pack: str) -> str:
    """Remove index entries whose target file does not exist in the pack.

    Measured 2026-07-29 on the upstream archive: ``rules-index.md`` advertises
    31 paths but ``rules/kinetic-beat-slam.md`` is not in the repo (the local
    checkout shows the same drift, so it is an upstream index bug, not a fetch
    failure). Handing that line to an author would recreate the exact defect
    this module exists to remove — a documented capability that resolves to
    nothing. An entry we cannot back with real text does not get advertised.
    """
    import re
    kept: list[str] = []
    for line in body.splitlines():
        m = re.search(r'path="([^"]+)"', line)
        if m and not os.path.isfile(os.path.join(_pack_dir(pack), m.group(1))):
            logger.debug('[Craft] dropping dead index entry %r', m.group(1))
            continue
        kept.append(line)
    return '\n'.join(kept)


def _design_index() -> str:
    """Synthesise a listing for the design pack, which ships no index.

    ``hyperframes-design`` has 13 frame presets under ``frame-presets/*/`` but
    no file advertising them, so without this they are on disk and invisible.
    Each ``FRAME.md`` opens with YAML frontmatter carrying ``name`` and
    ``description`` — enough to build the same name + summary shape the motion
    pack ships, addressable by the preset's directory name.
    """
    base = os.path.join(_pack_dir('hyperframes-design'), 'frame-presets')
    if not os.path.isdir(base):
        return ''
    lines: list[str] = []
    for preset in sorted(os.listdir(base)):
        path = os.path.join(base, preset, 'FRAME.md')
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                head = f.read(1200)
        except OSError as e:
            logger.debug('[Craft] cannot read %s: %s', path, e)
            continue
        # Frontmatter `description:` may be a folded (">") multi-line block.
        desc, capturing = '', False
        for raw in head.splitlines():
            line = raw.strip()
            if line.startswith('description:'):
                desc, capturing = line.split(':', 1)[1].strip(' >'), True
                continue
            if capturing:
                if not line or line.endswith(':') or line == '---':
                    break
                desc = f'{desc} {line}'.strip()
            if len(desc) > 240:
                break
        lines.append(f'<{preset} path="frame-presets/{preset}/FRAME.md">'
                     f'{desc[:240]}</{preset}>')
    if not lines:
        return ''
    return ('### hyperframes-design / frame presets\n'
            'Complete visual identities (palette, type scale, frame chrome). '
            'Read one before authoring to lock a consistent look.\n'
            '<presets>\n' + '\n'.join(lines) + '\n</presets>')


def craft_index(*, max_bytes: int = _MAX_INDEX_BYTES) -> str:
    """The pack INDEXES, concatenated — name + summary + tags per entry.

    This is what the author is handed WITH its prompt: enough to choose a rule,
    a blueprint or a frame preset by name, far too little to be a context bomb.
    The full text of a chosen entry comes from :func:`craft_reference`. Entries
    whose file is missing upstream are filtered — see :func:`_drop_dead_entries`.
    """
    if not craft_available():
        return ''
    chunks: list[str] = []
    for pack in CRAFT_PACKS:
        for name in _INDEX_FILES:
            path = os.path.join(_pack_dir(pack), name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    body = f.read()
            except OSError as e:
                logger.debug('[Craft] cannot read %s: %s', path, e)
                continue
            chunks.append(f'### {pack} / {name}\n'
                          f'{_drop_dead_entries(body, pack).strip()}')
    design = _design_index()
    if design:
        chunks.append(design)
    if not chunks:
        return ''
    out = '\n\n'.join(chunks)
    if len(out) > max_bytes:
        out = out[:max_bytes] + '\n…(index truncated)'
    return out


def _resolve(name: str) -> str:
    """Path of a reference by loose name, or ''. Never escapes the corpus.

    Matches a file stem (``kinetic-beat-slam`` → ``rules/kinetic-beat-slam.md``)
    OR a directory that holds a ``FRAME.md``, because design presets are
    addressed by their preset name (``biennale-yellow``) while the text lives
    one level down — the name the synthesised index prints must be the name
    that resolves.
    """
    token = (name or '').strip().lower().replace(' ', '-')
    if not token or '..' in token or token.startswith('/'):
        return ''
    token = os.path.splitext(token)[0]
    for pack in CRAFT_PACKS:
        base = _pack_dir(pack)
        if not os.path.isdir(base):
            continue
        for dirpath, _dirs, files in os.walk(base):
            if os.path.basename(dirpath).lower() == token \
                    and 'FRAME.md' in files:
                return os.path.join(dirpath, 'FRAME.md')
            for fname in files:
                if os.path.splitext(fname)[0].lower() == token:
                    return os.path.join(dirpath, fname)
    return ''


def craft_reference(name: str, *, max_bytes: int = _MAX_REFERENCE_BYTES) -> str:
    """The FULL text of one rule / blueprint / frame preset, or an honest miss.

    Resolution is by basename across the packs, so the author can pass exactly
    the token the index printed. A miss returns a message naming what to do
    next rather than an empty string: silence would read as "this rule has no
    content", which is the failure mode the whole epic is about.
    """
    if not craft_available():
        return ('The craft corpus is not available on this host — author from '
                'the composition contract and craft guide you were given.')
    path = _resolve(name)
    if not path:
        return (f'No craft reference named {name!r}. Use exactly the name the '
                f'index printed (e.g. "kinetic-beat-slam").')
    try:
        with open(path, encoding='utf-8') as f:
            body = f.read()
    except OSError as e:
        logger.warning('[Craft] cannot read %s: %s', path, e)
        return f'Could not read the reference {name!r}: {e}'
    if len(body) > max_bytes:
        body = body[:max_bytes] + '\n…(truncated)'
    rel = os.path.relpath(path, craft_root())
    return f'# craft reference: {rel}\n\n{body}'
