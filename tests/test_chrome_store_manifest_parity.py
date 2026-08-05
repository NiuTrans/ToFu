# Incident anchor: born in commit f9aa375c — fix(chrome-store): derive the store manifest from real API usage, not...
# (funeral audit pt_c565a36b3e8f42e6, docs/RATCHET_AUDIT.md)
"""tests/test_chrome_store_manifest_parity.py — the store build must declare
exactly the permissions the shipped code actually needs.

WHY THIS EXISTS
---------------
``docs/chrome-web-store/manifest.store.json`` is a HAND-MAINTAINED trim of
``browser_extension/manifest.json``: the store build swaps it in
(``scripts/package_extension.sh --store``) so reviewers are not asked to
justify permissions the code never calls. Two independent lists that must
agree, edited by hand, with the consequence of disagreement only visible
AFTER a store review — that is a drift generator.

It had already drifted, in the direction that hurts users:

  * ``downloads`` was MISSING from the store manifest while
    ``background.js::cmdDownload`` really calls ``chrome.downloads.download``
    and ``download`` is one of the extension's wire commands. Shipping that
    build meant the ``download`` command threw at runtime — for a user who
    installed from the store, silently and with no way to diagnose it.
  * the store manifest was pinned at version 4.3.0 while the shipped manifest
    had moved to 4.5.0, so the store build would carry 4.5.0's CODE under a
    4.3.0 label. ``package_extension.sh`` reads the version from whichever
    manifest it ships, so the zip name was wrong too.

THE INVARIANT
-------------
**The store manifest is DERIVED, not remembered.** These guards compute the
required permission set from the extension source and compare — so the next
time someone adds a ``chrome.X`` call, the guard names the permission they
forgot instead of a store reviewer (or a user) finding it later.

A NOTE ON ``activeTab`` — WHY "ZERO CALLS" IS THE WRONG TEST
------------------------------------------------------------
``activeTab`` has NO API surface: ``chrome.activeTab`` does not exist, so
grepping for calls to it always returns zero and proves nothing. It is a
*capability* permission, and per Chrome's own tabs documentation it is granted
only **on a user gesture** (clicking the action, a keyboard command, a context
menu) — and it exists here to widen ``tabs.captureVisibleTab`` to sensitive
targets (``chrome://`` pages, other extensions' pages, ``data:`` URLs).

Measured against this extension: there is no ``commands`` key, no
``context_menus`` key, and ``popup.js`` never triggers a capture. Every
screenshot arrives through ``executeAndReport`` from the server long-poll, so
NO user gesture ever precedes one. ``activeTab`` therefore can never actually
be granted, while ``<all_urls>`` (already declared) is what makes
``captureVisibleTab`` work for ordinary pages. Dropping it is right — but for
the reason above, not for a call count. The guard below encodes that reasoning
so nobody "restores" it by grepping.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = ROOT / 'browser_extension'
STORE_DIR = ROOT / 'docs' / 'chrome-web-store'
DEV_MANIFEST = EXT_DIR / 'manifest.json'
STORE_MANIFEST = STORE_DIR / 'manifest.store.json'
JUSTIFICATION = STORE_DIR / 'PERMISSIONS_JUSTIFICATION.md'
PACKAGE_SH = ROOT / 'scripts' / 'package_extension.sh'


def _json(p: Path) -> dict:
    return json.loads(p.read_text(encoding='utf-8'))


def _ext_sources() -> str:
    """Concatenated JS the store build actually ships.

    Mirrors the copy list in scripts/package_extension.sh — if that script
    starts shipping another file, this scan must follow it, which is why the
    file list is asserted separately below.
    """
    return '\n'.join(
        (EXT_DIR / n).read_text(encoding='utf-8')
        for n in ('background.js', 'popup.js') if (EXT_DIR / n).exists())


def _called_namespaces() -> set[str]:
    """Every ``chrome.<ns>`` namespace the shipped JS actually touches."""
    return set(re.findall(r'chrome\.([a-zA-Z]+)', _ext_sources()))


# A chrome.<namespace> call implies this manifest permission. Only namespaces
# that REQUIRE a declared permission appear here — `chrome.runtime` and
# `chrome.action` need none, so listing them would manufacture a false
# requirement.
_NS_REQUIRES_PERMISSION = {
    'tabs': 'tabs',
    'scripting': 'scripting',
    'storage': 'storage',
    'cookies': 'cookies',
    'history': 'history',
    'bookmarks': 'bookmarks',
    'debugger': 'debugger',
    'notifications': 'notifications',
    'alarms': 'alarms',
    'downloads': 'downloads',
    'webNavigation': 'webNavigation',
    'declarativeNetRequest': 'declarativeNetRequest',
    'management': 'management',
    'offscreen': 'offscreen',
}

# Permissions with NO callable namespace, kept or dropped on a reasoned basis
# rather than a call count. See the module docstring for activeTab.
_NO_API_SURFACE = {'activeTab', 'clipboardRead', 'clipboardWrite'}


def _required_permissions() -> set[str]:
    """The permission set the shipped code genuinely needs."""
    return {_NS_REQUIRES_PERMISSION[ns]
            for ns in _called_namespaces()
            if ns in _NS_REQUIRES_PERMISSION}


# ══════════════════════════════════════════════════════════
#  1. The store build must not break a command at runtime
# ══════════════════════════════════════════════════════════

def test_store_manifest_declares_every_permission_the_code_calls():
    """THE bug this file was written for.

    ``downloads`` was absent from the store manifest while ``cmdDownload``
    calls ``chrome.downloads.download``. A store-installed user hitting the
    ``download`` wire command got a runtime throw with no diagnosis path.
    """
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    missing = sorted(_required_permissions() - declared)
    assert not missing, (
        f"the store manifest omits {missing}, but the shipped background.js "
        f"really calls the matching chrome.* API. A store build would throw "
        f"at runtime for any user who triggers that command.")


def test_every_wire_command_has_its_permission_in_the_store_build():
    """Stated as the user-visible consequence: each wire command the server
    can send must be executable under the STORE permission set.

    ``download`` is the command that was broken; pinning the whole command
    surface keeps the next one from slipping through.
    """
    src = _ext_sources()
    commands = set(re.findall(r"case '([a-z_]+)':", src))
    assert 'download' in commands, (
        "the `download` wire command vanished — this guard's anchor is gone; "
        "re-point it at the current command surface rather than deleting it")
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    # cmdDownload is the one whose permission was missing; assert the link
    # explicitly so the failure message names the command, not just the API.
    assert 'downloads' in declared, (
        "the `download` wire command is dispatchable but the store manifest "
        "does not declare `downloads` — the command throws for store users")


# ══════════════════════════════════════════════════════════
#  2. No unjustifiable permission (store-review rejection)
# ══════════════════════════════════════════════════════════

def test_store_manifest_declares_nothing_the_code_never_uses():
    """A permission the code never exercises cannot be justified to a
    reviewer, and `management` / `declarativeNetRequest` are outright red
    flags. The complement of test 1 — together they pin the set exactly."""
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    callable_perms = set(_NS_REQUIRES_PERMISSION.values())
    # Only judge permissions that HAVE an API surface; activeTab et al. are
    # decided on reasoning (see the dedicated test below).
    unused = sorted((declared & callable_perms) - _required_permissions())
    assert not unused, (
        f"the store manifest declares {unused}, which the shipped code never "
        f"calls. Each one is an unjustifiable box on the Privacy practices "
        f"tab and a rejection risk.")


def test_active_tab_stays_out_because_no_user_gesture_can_grant_it():
    """``activeTab`` is gesture-granted; this extension has no gesture path.

    Asserted from the FACTS that make it true (no commands key, no
    context_menus, popup never captures), not from a call count — because
    ``chrome.activeTab`` does not exist and grepping for it proves nothing.
    If someone later adds a keyboard command or a popup-driven capture, this
    test fails and the permission should be reconsidered on its merits.
    """
    dev = _json(DEV_MANIFEST)
    assert 'commands' not in dev and 'context_menus' not in dev, (
        "the extension gained a gesture entry point (commands/context_menus). "
        "activeTab may now be grantable — re-evaluate whether the store "
        "manifest should declare it, and update this guard's reasoning.")
    popup = (EXT_DIR / 'popup.js').read_text(encoding='utf-8')
    assert 'captureVisibleTab' not in popup and 'screenshot' not in popup, (
        "popup.js now triggers a capture from a real user gesture, which is "
        "exactly the case activeTab exists for — re-evaluate.")
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    assert 'activeTab' not in declared, (
        "activeTab is declared but can never be granted here: every command "
        "arrives from the server long-poll, never from a user gesture, and "
        "<all_urls> already covers captureVisibleTab for ordinary pages. "
        "It is an unjustifiable permission on the review form.")


def test_host_permissions_cover_capture_on_ordinary_pages():
    """Complement to dropping activeTab: `captureVisibleTab` still needs
    `<all_urls>` OR `activeTab`. Removing both would silently break every
    screenshot, which is the failure this whole file exists to prevent."""
    src = _ext_sources()
    if 'captureVisibleTab' not in src:
        pytest.skip('extension no longer captures the visible tab')
    hosts = _json(STORE_MANIFEST).get('host_permissions', [])
    assert '<all_urls>' in hosts, (
        f"captureVisibleTab is used and activeTab is (correctly) not declared, "
        f"so <all_urls> is what keeps screenshots working — but "
        f"host_permissions is {hosts}. Screenshots would fail for every user.")


# ══════════════════════════════════════════════════════════
#  3. Version parity — the store zip must not mislabel code
# ══════════════════════════════════════════════════════════

def test_store_manifest_version_matches_the_shipped_code():
    """The store manifest sat at 4.3.0 while the code moved to 4.5.0.

    ``package_extension.sh`` derives both the shipped version AND the zip
    filename from whichever manifest it uses, so the drift produced a build
    labelled 4.3.0 that actually contained 4.5.0's background.js — an
    update users could never receive correctly.
    """
    dev_v = _json(DEV_MANIFEST).get('version')
    store_v = _json(STORE_MANIFEST).get('version')
    assert store_v == dev_v, (
        f"store manifest says {store_v!r} but the shipped extension is "
        f"{dev_v!r}. The store build would carry the newer code under an "
        f"older version label.")


def test_the_two_manifests_agree_on_everything_except_permissions():
    """The store manifest is a PERMISSION trim — nothing else may diverge.

    Any other drift (name, CSP, background entry, icons) means the store
    build behaves differently from the tested one, which is the same class of
    defect as the version skew above.
    """
    dev, store = _json(DEV_MANIFEST), _json(STORE_MANIFEST)
    for key in ('manifest_version', 'name', 'background', 'action',
                'content_security_policy', 'icons', 'host_permissions'):
        assert store.get(key) == dev.get(key), (
            f"manifest key {key!r} differs between the dev and store builds "
            f"({dev.get(key)!r} vs {store.get(key)!r}). Only `permissions` "
            f"may be trimmed.")


# ══════════════════════════════════════════════════════════
#  4. The submission paperwork must match the manifest
# ══════════════════════════════════════════════════════════

def _justification_blocks() -> set[str]:
    """Permission names that have a `## \\`name\\`` justification heading."""
    txt = JUSTIFICATION.read_text(encoding='utf-8')
    return set(re.findall(r'^## `([A-Za-z]+)`\s*$', txt, re.M))


def test_every_declared_permission_has_a_justification_block():
    """The dashboard shows one mandatory text box per declared permission.
    A missing block means submitting with an empty justification, which is a
    documented rejection cause."""
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    missing = sorted(declared - _justification_blocks())
    assert not missing, (
        f"declared in the store manifest but with no justification block in "
        f"PERMISSIONS_JUSTIFICATION.md: {missing}. The reviewer sees an empty "
        f"box.")


def test_no_justification_block_for_a_permission_we_do_not_ask_for():
    """The reverse drift: a block for a permission we dropped tells whoever
    submits to paste a justification for something not in the manifest, which
    is how a dropped permission gets re-added by accident."""
    declared = set(_json(STORE_MANIFEST).get('permissions', []))
    orphans = sorted(_justification_blocks() - declared)
    assert not orphans, (
        f"PERMISSIONS_JUSTIFICATION.md still has justification blocks for "
        f"{orphans}, which the store manifest no longer declares. Delete them "
        f"or they invite re-adding the permission.")


def test_the_removed_permissions_table_matches_reality():
    """The doc's "REMOVED for the store build" table is what a human reads
    before submitting. It said 6; the real trim is larger. A stale count
    there is how the activeTab decision silently got lost."""
    txt = JUSTIFICATION.read_text(encoding='utf-8')
    dev = set(_json(DEV_MANIFEST).get('permissions', []))
    store = set(_json(STORE_MANIFEST).get('permissions', []))
    actually_removed = sorted(dev - store)
    listed = set(re.findall(r'^\| `([A-Za-z]+)` \|', txt, re.M))
    missing = sorted(set(actually_removed) - listed)
    assert not missing, (
        f"these permissions are trimmed from the store build but the "
        f"REMOVED table never explains why: {missing}. Whoever submits has "
        f"no record of the decision.")
    stale = sorted(listed - set(actually_removed))
    assert not stale, (
        f"the REMOVED table lists {stale}, which are NOT actually removed "
        f"from the store manifest — the table and the manifest disagree.")


def test_no_document_states_a_hardcoded_removed_count():
    """A prose count ("removes 6 permissions") goes stale the moment the trim
    changes, and it already did. The table above is the single source of
    truth; the prose must not restate a number that can drift from it.

    BOTH word orders are matched. The first version of this guard only caught
    verb-first phrasing ("removes the 6 permissions") and stayed green while
    the kit README said "(6 unused permissions removed)" — a guard that
    passes while the defect it names is still on the page.
    """
    verb_first = re.compile(
        r'(?:removes?|removed|dropp?e?d?|trims?|trimmed)\s+(?:the\s+)?'
        r'(\d+)\s+(?:unused\s+)?permissions?', re.I)
    count_first = re.compile(
        r'(\d+)\s+(?:unused\s+)?permissions?\s+'
        r'(?:removed|dropped|trimmed|are\s+removed)', re.I)
    offenders = {}
    for p in list(STORE_DIR.glob('*.md')) + [PACKAGE_SH]:
        if not p.exists():
            continue
        txt = p.read_text(encoding='utf-8')
        hits = verb_first.findall(txt) + count_first.findall(txt)
        if hits:
            offenders[p.name] = hits
    assert not offenders, (
        f"these files hardcode a permission-removal COUNT: {offenders}. The "
        f"count drifts whenever the trim changes (it already read 6 while the "
        f"real trim was 7). Say 'the permissions listed below' and let the "
        f"table be the source of truth.")


def test_the_kit_documents_the_edge_addons_route():
    """Edge Add-ons must be a first-class target, not an afterthought.

    The backend drives Edge and the READMEs tell users it works, but a store
    listing is the only path that gives a NON-developer one-click install —
    and Edge is the one that costs nothing, accepts individual accounts, and
    takes the exact same zip. A kit that documents only Chrome silently makes
    the cheapest real route invisible.
    """
    edge = STORE_DIR / 'EDGE_ADDONS.md'
    assert edge.exists(), (
        'docs/chrome-web-store/EDGE_ADDONS.md is missing — the Edge Add-ons '
        'submission route is undocumented, so the zero-fee path to one-click '
        'install exists only in someone\'s head')
    txt = edge.read_text(encoding='utf-8')
    for needle, why in (
            ('partner.microsoft.com', 'the Partner Center registration URL'),
            ('Individual', 'that individual (non-company) accounts are supported'),
            ('remote code', "Edge's stricter MV3 remote-code rule"),
    ):
        assert needle in txt, f'EDGE_ADDONS.md never states {why}'


def test_the_edge_route_is_discoverable_from_the_kit_entry_points():
    """A doc nobody is pointed at is a doc nobody reads. The kit README and
    the Chrome checklist must both hand the reader the Edge route."""
    for name in ('README.md', 'SUBMISSION_CHECKLIST.md'):
        txt = (STORE_DIR / name).read_text(encoding='utf-8')
        assert 'EDGE_ADDONS.md' in txt, (
            f'{name} never links EDGE_ADDONS.md, so the Edge route is only '
            f'findable by listing the directory')


def test_the_fallback_ladder_puts_edge_before_firefox():
    """Ordering is the whole point of the ladder.

    Firefox needs a real code port (no `chrome.debugger`) AND a signing
    pipeline, because it has no persistent unpacked install (charter #20).
    Edge needs neither — same package, no fee. A ladder that names Firefox as
    the next stop after Chrome sends the reader down the most expensive path
    first, which is what this file used to do.
    """
    txt = (STORE_DIR / 'REVIEW_RISKS.md').read_text(encoding='utf-8')
    ladder = txt[txt.index('## Realistic outcome ladder'):]
    edge_at = ladder.find('Edge Add-ons')
    ff_at = ladder.find('Firefox')
    assert edge_at != -1, 'the outcome ladder never mentions Edge Add-ons'
    assert ff_at != -1, 'the outcome ladder no longer mentions Firefox'
    assert edge_at < ff_at, (
        'the outcome ladder reaches Firefox before Edge Add-ons. Edge is the '
        'same package with no fee; Firefox is a code port plus an AMO signing '
        'pipeline. Order them by real cost.')


def test_the_kit_does_not_sell_edge_as_a_free_pass_on_remote_code():
    """Honesty guard, and the reason it is worth a test: it would be easy to
    present Edge as "same package, zero cost, done". Microsoft's MV3 rule on
    remotely hosted code is worded MORE absolutely than Chrome's, and this
    extension runs server-sent JS. If the kit ever drops that caveat, whoever
    submits will be blindsided by the same rejection twice."""
    txt = (STORE_DIR / 'EDGE_ADDONS.md').read_text(encoding='utf-8')
    assert 'not permitted' in txt or 'stricter' in txt.lower() or 'STRICTER' in txt, (
        'EDGE_ADDONS.md no longer warns that Edge is stricter than Chrome on '
        'remote code under MV3 — the kit now oversells the Edge route')


def test_the_popup_version_badge_is_derived_not_remembered():
    """The popup badge sat at ``v4.3`` while the manifest moved on — a
    hardcoded twin nobody bumped (user-reported 2026-08-02: the extension
    page said 4.5.x, the popup said v4.3, and it read as "did my update
    not land?"). The badge is now filled from ``chrome.runtime.getManifest``
    at popup open so it CANNOT drift; these pins keep it derived.

    Same drift family as ``test_store_manifest_version_matches_the_shipped_code``
    and ``test_no_document_pins_a_stale_extension_version`` — one version
    fact, one owner, every display derived.
    """
    html = (EXT_DIR / 'popup.html').read_text(encoding='utf-8')
    assert not re.search(r'class="version"[^>]*>\s*v?\d', html), (
        'popup.html hardcodes a version badge again — it goes stale on the '
        'very next manifest bump. The badge is derived from the manifest at '
        'runtime; keep it that way.')
    assert 'id="versionBadge"' in html, (
        'popup.html lost the versionBadge anchor popup.js fills')
    js = (EXT_DIR / 'popup.js').read_text(encoding='utf-8')
    assert 'versionBadge' in js and 'getManifest().version' in js, (
        'popup.js no longer fills the badge from chrome.runtime.getManifest()'
        ' — the badge is static again, which is exactly the drift this guard '
        'exists to prevent')


def test_no_document_pins_a_stale_extension_version():
    """SUBMISSION_CHECKLIST told the submitter to look for a
    ``4.3.0-store.zip`` that ``package_extension.sh`` would never produce
    once the version moved — a checklist step that can only fail."""
    dev_v = _json(DEV_MANIFEST).get('version')
    offenders = {}
    for p in STORE_DIR.glob('*.md'):
        txt = p.read_text(encoding='utf-8')
        stale = sorted({v for v in re.findall(r'\b(\d+\.\d+\.\d+)-store\b', txt)
                        if v != dev_v}
                       | {v for v in re.findall(r'tofu-browser-bridge-(\d+\.\d+\.\d+)', txt)
                          if v != dev_v})
        if stale:
            offenders[p.name] = stale
    assert not offenders, (
        f"store docs pin extension versions that no longer exist (shipped is "
        f"{dev_v}): {offenders}. The build produces a different filename, so "
        f"the checklist step cannot be completed as written.")


# ══════════════════════════════════════════════════════════
#  5. The packaging script must ship what we audited
# ══════════════════════════════════════════════════════════
#
# `scripts/package_extension.sh` is the ONLY build path for the store zip, and
# the kit that instructs the reader to run it SHIPS in the opensource export
# (docs/ is not stripped). It therefore has to survive BOTH gates — git and
# export — or the public tree documents a command it does not contain. It
# previously survived neither: `.gitignore`'s blanket `/scripts/*` left it
# untracked, so a clean clone could not build at all. Fixed by the same
# convention the four sibling scripts use (a `!` exception plus an
# export._OPENSOURCE_KEEP_FILES entry); the guards below are the ratchet that
# keeps it that way, per charter #14's two-door rule.


def test_the_store_build_tool_survives_git_and_export():
    """The shipped checklist says "run scripts/package_extension.sh --store".

    Both doors are asserted because they fail independently and each one alone
    still leaves the instruction dead: untracked → absent from a clean clone;
    export-stripped → absent from the public tree that carries the checklist.
    """
    rel = 'scripts/package_extension.sh'
    checklist = (STORE_DIR / 'SUBMISSION_CHECKLIST.md').read_text(encoding='utf-8')
    assert 'package_extension.sh' in checklist, (
        'the checklist no longer references the packaging script — re-point '
        'this guard at whatever the build step is now, do not delete it')

    tracked = subprocess.run(
        ['git', 'ls-files', '--error-unmatch', rel],
        cwd=ROOT, capture_output=True, text=True, timeout=60).returncode == 0
    assert tracked, (
        f'{rel} is not tracked by git, so a clean clone cannot run the store '
        f'build the shipped checklist prescribes. Add a `!` exception to '
        f'.gitignore next to the four sibling scripts.')

    export = pytest.importorskip(
        'export', reason='export.py is itself stripped from public trees')
    assert rel in export._OPENSOURCE_KEEP_FILES, (
        f'{rel} lives under the opensource-excluded scripts/ dir and is NOT in '
        f'export._OPENSOURCE_KEEP_FILES, so the public tree ships a checklist '
        f'whose build step is missing (charter #13: export artifacts are a '
        f'first-class acceptance target).')


def test_package_script_ships_exactly_the_files_we_scanned():
    """These guards scan background.js + popup.js. If the store build starts
    shipping another JS file, the permission scan above goes blind to it —
    so the file list is itself an invariant."""
    sh = PACKAGE_SH.read_text(encoding='utf-8')
    # Match the copy SOURCE only up to the filename — `\.js` alone also
    # matched inside `manifest.json`, inventing a `manifest.js` that does not
    # exist. Anchor on a word boundary so `.json` cannot be truncated to `.js`.
    copied_js = set(re.findall(r'\$SRC_DIR/([A-Za-z0-9_-]+\.js)\b', sh))
    scanned = {'background.js', 'popup.js'}
    assert copied_js == scanned, (
        f"package_extension.sh ships JS {sorted(copied_js)} but the "
        f"permission scan in this file only reads {sorted(scanned)}. Update "
        f"_ext_sources() or the scan misses whatever the new file calls.")


def test_package_script_swaps_in_the_audited_store_manifest():
    """The whole audit is worthless if --store does not actually use the file
    these tests check."""
    sh = PACKAGE_SH.read_text(encoding='utf-8')
    assert 'manifest.store.json' in sh, (
        "package_extension.sh no longer references manifest.store.json — the "
        "audited manifest is not what gets shipped")


def test_the_store_manifest_is_tracked_by_git():
    """A store asset that git ignores cannot be reviewed, shared, or shipped
    from a clean clone — and a guard on an untracked file passes vacuously.
    (This repo has a live precedent: docs/README_EXTENSION.md is gitignored,
    so a guard aimed at it would never have run on CI.)
    """
    out = subprocess.run(
        ['git', 'ls-files', '--error-unmatch',
         str(STORE_MANIFEST.relative_to(ROOT))],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, (
        f"{STORE_MANIFEST.relative_to(ROOT)} is not tracked by git, so these "
        f"guards would pass vacuously on a clean checkout and the store build "
        f"would have no manifest to swap in.")


# ══════════════════════════════════════════════════════════
#  6. The PAUSED decision must stay recorded
# ══════════════════════════════════════════════════════════
#
# 2026-07-31 the owner decided NOT to submit: stay on "load unpacked". The
# blocker was never the paperwork — every mechanical precondition in this file
# is green — it was the trade-off in REVIEW_RISKS.md ("Decision to make NOW"):
# the only realistic route to acceptance is a REDUCED build with
# browser_execute_js and debugger removed, which narrows what the extension can
# do.
#
# A kit that reads "here is how to submit" with no record of a decision NOT to
# is an open invitation to redo the whole investigation. The guards below keep
# the decision attached to the artifact it governs — the same reason the
# activeTab reasoning is pinned above rather than left in a commit message.


def test_the_kit_entry_point_records_the_do_not_submit_decision():
    """The reader must meet the decision before the instructions.

    Placement is asserted, not just presence: a note further down the page is
    reached only by someone already following the checklist.
    """
    txt = (STORE_DIR / 'README.md').read_text(encoding='utf-8')
    head = txt[:2500]
    assert re.search(r'NOT SUBMITTING|不上架|do not submit', head, re.I), (
        'docs/chrome-web-store/README.md no longer states up front that the '
        'owner decided against submitting. Without it the kit reads as an '
        'open task and the next reader re-runs a closed investigation. If the '
        'decision was REVERSED, delete this guard in the same commit that '
        'starts the submission — do not quietly soften the note.')
    assert '2026-07-31' in head, (
        'the do-not-submit note carries no date, so a reader cannot tell '
        'whether it predates their own information.')


def test_the_paused_decision_names_the_tradeoff_that_caused_it():
    """"We decided no" without the WHY decays into folklore.

    The reason is specific and load-bearing: acceptance realistically requires
    dropping browser_execute_js + debugger. Anyone revisiting this needs that
    to be the first thing they see, because it is a CODE change, not a
    paperwork one.

    Scoped to the NOTE BLOCK, not the whole file. A first version searched the
    entire README and stayed green after the note was deleted outright,
    because `browser_execute_js` and `debugger` both appear in the honest
    expectation-setting paragraph further down — a guard that passes while the
    thing it guards is gone.
    """
    txt = (STORE_DIR / 'README.md').read_text(encoding='utf-8')
    if 'NOT SUBMITTING' not in txt:
        pytest.fail(
            'no do-not-submit note to check — see the sibling guard, which '
            'owns that failure')
    start = txt.index('NOT SUBMITTING')
    note = txt[start:txt.index('Everything needed to publish', start)]
    for needle, why in (
            ('browser_execute_js', 'the remote-code capability at stake'),
            ('debugger', 'the second permission a reduced build would drop'),
            ('REVIEW_RISKS.md', 'where the full trade-off is written up'),
    ):
        assert needle in note, (
            f'the do-not-submit note never mentions {needle} — {why}. Without '
            f'it the decision looks arbitrary and will be re-litigated from '
            f'scratch.')


def test_the_paused_kit_still_claims_only_what_is_measurably_true():
    """The readiness facts in the note must match the actual artifacts.

    A parked kit is exactly where stale claims survive unnoticed, because
    nobody exercises it. These are re-derived from the manifests rather than
    trusted as prose, so the note cannot drift into telling a future submitter
    something false about the state they are inheriting.
    """
    txt = (STORE_DIR / 'README.md').read_text(encoding='utf-8')
    if 'Kit readiness' not in txt:
        pytest.skip('the note no longer makes readiness claims')
    store = _json(STORE_MANIFEST)
    n = len(store.get('permissions', []))
    assert f'{n} permissions' in txt, (
        f'the readiness note does not state the real permission count '
        f'({n}). A parked kit with a wrong count is how the next submitter '
        f'starts from a false premise.')
    assert store.get('version') in txt, (
        f"the readiness note does not name the current store-manifest version "
        f"({store.get('version')!r}), so it cannot be trusted as a snapshot.")


def test_the_note_does_not_contradict_the_closed_install_routes():
    """The 'one exe installs the extension' idea stays closed on FACTS.

    Those facts are what make the manual three-step install non-negotiable, so
    the note must carry them: if they are lost, someone re-proposes an
    installer-driven extension install and burns the same investigation again.
    """
    txt = (STORE_DIR / 'README.md').read_text(encoding='utf-8')
    for needle in ('update_url', 'force_installed', '--load-extension'):
        assert needle in txt, (
            f'the note omits {needle!r}, one of the three measured reasons an '
            f'installer cannot place the extension. All three belong together '
            f'— each alone leaves an apparent loophole.')
