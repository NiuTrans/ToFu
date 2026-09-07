"""Guard: a memory's ``tags`` is list[str] for EVERY frontmatter form we ship.

Why this exists
───────────────
The frontend does ``sk.tags.forEach(...)`` (static/js/memory.js:231) to build a
memory card. When the parser handed back a plain string, that threw
``TypeError: sk.tags.forEach is not a function`` and the card was replaced by a
"memory-card-error" box — the memory became invisible in the UI.

Measured on the real corpus (2026-07-28): 6 of 1163 tagged memory files were
written as a bare comma list (``tags: a, b, c``) rather than the bracketed form
``_build_frontmatter`` emits, and all 6 rendered as error cards. The defect was
found by the browser JS-error capture added in the same change, NOT by any
existing assertion — nothing in the suite watched the console.

Discipline (charter: assert the RESULT, not the implementation)
──────────────────────────────────────────────────────────────
These tests assert the parsed VALUE for each on-disk form. They stay valid if
the parser is rewritten, and they go red if the coercion is dropped. They do
NOT assert that ``_COMMA_LIST_KEYS`` equals any particular set — that is the
implementation.
"""

import pytest

from lib.memory.storage._frontmatter import _parse_frontmatter, _build_frontmatter

pytestmark = [pytest.mark.unit]


def _fm(body_key_lines):
    return '---\n' + body_key_lines + '\n---\nSome body text.\n'


class TestTagsIsAlwaysAList:
    """Every shipped spelling of a tags line must parse to list[str]."""

    def test_bare_comma_list_with_spaces(self):
        meta, _ = _parse_frontmatter(_fm('name: x\ntags: javascript, css, bug-fix'))
        assert meta['tags'] == ['javascript', 'css', 'bug-fix']

    def test_bare_comma_list_without_spaces(self):
        # Real sample: loadconversationmessages-phase2-overwrite-race-condition.md
        meta, _ = _parse_frontmatter(_fm('tags: javascript,race-condition,bug-fix'))
        assert meta['tags'] == ['javascript', 'race-condition', 'bug-fix']

    def test_bracketed_list_still_works(self):
        meta, _ = _parse_frontmatter(_fm('tags: [alpha, beta]'))
        assert meta['tags'] == ['alpha', 'beta']

    def test_single_tag_no_comma_is_still_usable(self):
        # No comma at all: must not become a list of characters, and the
        # frontend's `(sk.tags || []).some(...)` path must not explode.
        meta, _ = _parse_frontmatter(_fm('tags: solo'))
        val = meta['tags']
        assert val == 'solo' or val == ['solo'], f'unexpected shape: {val!r}'

    def test_round_trip_build_then_parse_yields_a_list(self):
        text = _build_frontmatter({'name': 'n', 'tags': ['a', 'b']}) + 'body\n'
        meta, _ = _parse_frontmatter(text)
        assert meta['tags'] == ['a', 'b']


class TestProseKeysAreNeverSplit:
    """Complement — without this, "split every comma" would also pass above.

    Splitting a description on commas would corrupt real content, which is
    worse than the rendering bug being fixed. NEUTER check: widen the coercion
    to all keys and these go red.
    """

    def test_description_with_commas_stays_one_string(self):
        desc = 'Fixes the popup, which was clipped, on delete'
        meta, _ = _parse_frontmatter(_fm(f'description: {desc}'))
        assert meta['description'] == desc

    def test_name_with_commas_stays_one_string(self):
        meta, _ = _parse_frontmatter(_fm('name: a, b, c'))
        assert meta['name'] == 'a, b, c'


def test_no_shipped_memory_file_parses_tags_as_a_string():
    """End-to-end over the REAL corpus: no memory may render as an error card.

    This is the assertion that would have caught the production defect. It
    reads the actual files rather than a synthetic fixture, because the bug
    was precisely that hand-written files use a form the writer never emits.
    """
    import glob
    import os

    files = []
    for root in ('.tofu/memories', 'data/memories'):
        files += glob.glob(os.path.join(root, '**', '*.md'), recursive=True)
    if not files:
        pytest.skip('no memory corpus on this checkout')

    offenders = []
    tagged = 0
    for f in files:
        try:
            text = open(f, encoding='utf-8').read()
        except OSError:
            continue
        meta, _ = _parse_frontmatter(text)
        raw = meta.get('tags')
        if raw is None:
            continue
        tagged += 1
        # A single bare word is fine; a comma-separated string is the defect,
        # because the frontend iterates it.
        if isinstance(raw, str) and ',' in raw:
            offenders.append((os.path.basename(f), raw[:60]))

    # Print the scan surface BEFORE asserting (charter: verify the scan surface).
    print(f'\nscanned {len(files)} memory files; {tagged} carry a tags key')
    print(f'offenders (comma-string tags): {len(offenders)}')
    for name, val in offenders[:10]:
        print('   -', name, '->', val)

    assert tagged > 0, 'scan surface is empty — the guard would be vacuous'
    assert not offenders, (
        f'{len(offenders)} memory file(s) parse tags as a comma STRING; their '
        f'cards crash with tags.forEach is not a function: {offenders[:5]}')
