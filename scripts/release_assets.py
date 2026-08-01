#!/usr/bin/env python3
"""The single source of truth for "is a Tofu release COMPLETE?".

WHY THIS FILE EXISTS
--------------------
Two gates in ``.github/workflows/build-desktop.yml`` need the same answer, at
opposite ends of the pipeline:

  * the ``version`` job asks it about a release that ALREADY EXISTS on the
    remote, to decide whether v<VERSION> still needs building;
  * the ``release`` job asks it about the artifacts it just downloaded, to
    refuse to publish a partial set.

If each gate carried its own copy of the expected-asset list, adding a
platform would silently fix one and leave the other behind — and the failure
would be invisible, because both copies keep passing on the platforms they
still know about. So the list lives here, once, and both gates shell out to
this script.

THE DEFECT THIS CLOSES
----------------------
The ``version`` gate used to treat *"GET /releases/tags/v$VER returned 200"*
as proof that the version had shipped. It is not. A release object can exist
with **no assets at all**: ``action-gh-release`` creates the release first and
uploads afterwards, so any interruption in between (a cancelled run, a dead
runner, a network blip, an internal action failure) leaves a release that is
addressable, tagged, and empty.

After that, the gate reads 200 forever and the version can never rebuild —
without a human deleting the release or bumping VERSION. The self-heal path
does not exist.

This is the same shape as the defect fixed in 5114cbca ("a tag is a PRODUCT of
releasing, not evidence of it"), one level down: the gate was upgraded from
*"does the tag exist?"* to *"does the Release exist?"*, which is the right
direction but still a proxy. What a user actually needs is *"can I download an
installer for my platform?"* — and only the asset list answers that.

THREE-STATE ON PURPOSE
----------------------
``COMPLETE`` / ``INCOMPLETE`` / ``UNDETERMINED`` are distinct exit codes
because the callers must treat them differently, and collapsing the third into
either of the others is how a fail-open rule gets lost:

  * the ``version`` gate builds on INCOMPLETE **and** on UNDETERMINED — a
    redundant build costs four runners, a missed one is a silent non-release;
  * the retarget step moves a tag on INCOMPLETE but NOT on UNDETERMINED —
    force-moving a tag that turns out to have been published is destructive
    and hard to undo.

Same asymmetry the two gates already use for their HTTP probes, for the same
reason: uncertainty resolves toward the cheap mistake, never the expensive one.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

# ── The expected assets, in ONE place ─────────────────────────────
#
# Each entry is (os key, arch, human label, glob). The globs match the
# filenames the build jobs actually upload — see the ``Upload …`` steps in
# build-desktop.yml.
#
# Adding a platform means adding ONE line here; both gates pick it up. That is
# the entire point of this module, so resist inlining any of these patterns
# back into the workflow (tests/test_desktop_build_workflow.py asserts that
# they appear in no other file).
#
# ── Why the os/arch keys are part of the DATA, not parsed from the name ──
# A third consumer joined the two release gates: ``routes/api_v1/desktop.py``
# answers "which of these files does THIS visitor's machine take?", so that the
# in-app download is one click on the right installer instead of a releases
# page with five assets on it and a guess.
#
# That consumer needs a machine-readable platform, and the two obvious ways to
# get one are both a second copy of this mapping in disguise:
#
#   * regex the GLOB (``'win64' in pattern``) — couples the route to a naming
#     convention nothing enforces, and silently mis-sorts the first asset that
#     spells a platform differently;
#   * regex the LABEL — worse, because labels are prose meant for humans and
#     nothing stops one being reworded.
#
# Either way the platform mapping would live in two places while LOOKING like
# it lived in one, which is the failure mode this whole module exists to
# prevent. So the platform IS a field.
#
# ``arch`` is the architecture the asset RUNS ON, which for macOS is the one
# fact a browser cannot tell us: Apple Silicon Macs report ``Intel Mac OS X``
# in their UA string, so the two DMGs are indistinguishable without the
# ``Sec-CH-UA-Arch`` client hint. Hence two separate entries rather than one
# "macOS" row — the route offers both when it cannot know, and narrows to one
# when the hint arrives.
#
# ── Why a SIZE FLOOR is part of the same row ──
# Counting filenames is not enough, and this was measured, not theorised. On
# run 30601806258 the Windows leg's `pip install -r requirements.txt` failed
# while the step reported success (pwsh discards an earlier command's exit
# code), so PyInstaller packaged an app with the project's dependencies
# MISSING and still produced a correctly-NAMED installer:
#
#     hollow build   Tofu-Setup-…-win64.exe    48,960,018 bytes
#     healthy v0.14.2 Tofu-Setup-0.14.2-win64.exe  115,822,886 bytes
#
# To a name-only gate those two are identical. That release was saved purely
# by the other three legs failing too — had they passed, `make_latest` would
# have pinned a 49 MB installer containing essentially none of Tofu as Latest.
#
# The floors below are 70% of the measured v0.14.2 sizes: loose enough that
# ordinary release-to-release drift (a dropped dependency, better compression)
# never trips them, tight enough that the 49 MB hollow build is caught with
# room to spare (its floor is 81 MB). This bounds a CLASS of failure — any
# build that silently omits a large chunk of the app — not just the pwsh path,
# which is why it belongs here rather than only in the workflow's shell fix.
PLATFORM_ASSETS: tuple[tuple[str, str, str, str, int], ...] = (
    ('macos',   'arm64',  'macOS arm64 DMG',   'Tofu-*-macos-arm64.dmg',  119_000_000),
    ('macos',   'x86_64', 'macOS x86_64 DMG',  'Tofu-*-macos-x86_64.dmg', 121_000_000),
    ('windows', 'x86_64', 'Windows installer', 'Tofu-Setup-*-win64.exe',   81_000_000),
    ('linux',   'x86_64', 'Linux archive',     'Tofu-*-linux*.tar.gz',    135_000_000),
)

# The (label, glob) view the two release gates consume. DERIVED, never
# maintained alongside PLATFORM_ASSETS — two hand-kept lists would drift, and
# both would keep passing on the platforms they still knew about.
REQUIRED_PLATFORM_ASSETS: tuple[tuple[str, str], ...] = tuple(
    (label, pattern) for _os, _arch, label, pattern, _min in PLATFORM_ASSETS)

# Generated by the release job AFTER the local completeness gate runs, so it is
# required only when auditing a PUBLISHED release. Users are told to verify
# downloads with `shasum -a 256 -c SHA256SUMS`, so a release without it is
# incomplete even when every installer is present.
CHECKSUMS_ASSET = 'SHA256SUMS'

EXIT_COMPLETE = 0
EXIT_INCOMPLETE = 1
EXIT_UNDETERMINED = 2


def missing_assets(names, *, require_checksums: bool) -> list[str]:
    """Return the labels of every required asset that ``names`` lacks.

    Args:
        names: asset filenames present (any iterable of str).
        require_checksums: also require ``SHA256SUMS``. True when auditing a
            published release; False for the release job's local gate, which
            runs before the checksum file is generated.

    Returns:
        Human-readable labels of the missing assets — empty means complete.
    """
    have = [str(n) for n in names if str(n).strip()]
    gaps: list[str] = []
    for label, pattern in REQUIRED_PLATFORM_ASSETS:
        if not any(fnmatch.fnmatch(n, pattern) for n in have):
            gaps.append(f'{label} ({pattern})')
    if require_checksums and not any(n == CHECKSUMS_ASSET for n in have):
        gaps.append(f'checksums ({CHECKSUMS_ASSET})')
    return gaps


def undersized_assets(sizes: dict) -> list[str]:
    """Return a report line for every present asset that is implausibly small.

    Args:
        sizes: mapping of asset filename -> size in bytes. Assets that are
            absent are NOT reported here — that is ``missing_assets``' job, and
            reporting the same gap twice would make a missing platform look
            like two independent failures.

    Returns:
        Human-readable lines, empty when every present asset clears its floor.
    """
    gaps: list[str] = []
    for _os, _arch, label, pattern, min_bytes in PLATFORM_ASSETS:
        for name, size in sorted(sizes.items()):
            if not fnmatch.fnmatch(name, pattern):
                continue
            if int(size) < min_bytes:
                gaps.append(
                    f'{label}: {name} is {int(size):,} bytes, below the '
                    f'{min_bytes:,} floor — the build very likely omitted the '
                    'app\'s dependencies (a hollow 49 MB Windows installer was '
                    'produced exactly this way on run 30601806258 while every '
                    'step reported success)')
    return gaps


def names_from_release_json(text: str) -> list[str] | None:
    """Extract ``assets[].name`` from a GitHub release payload.

    Returns:
        The asset names, or ``None`` if the payload is not a release object —
        which the caller must treat as UNDETERMINED rather than as "no assets".
        Reading a truncated or error-shaped body as an empty asset list would
        turn a transient API hiccup into a confident "this release is broken",
        and on the retarget path that would force-move a published tag.
    """
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None
    assets = doc.get('assets')
    if not isinstance(assets, list):
        # A release object always carries an `assets` array (empty when the
        # upload never happened). Its ABSENCE means this is not a release
        # payload at all — e.g. {"message": "Not Found"} or a proxy error page.
        return None
    out: list[str] = []
    for a in assets:
        if isinstance(a, dict) and isinstance(a.get('name'), str):
            out.append(a['name'])
    return out


def sizes_from_release_json(text: str) -> dict | None:
    """Extract ``{name: size}`` from a GitHub release payload.

    Same UNDETERMINED contract as :func:`names_from_release_json`: ``None``
    when the body is not a release object, so a truncated response can never be
    read as "every asset is 0 bytes" — which would report a healthy published
    release as hollow and, on the retarget path, move a published tag.
    """
    try:
        doc = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get('assets'), list):
        return None
    out: dict = {}
    for a in doc['assets']:
        if (isinstance(a, dict) and isinstance(a.get('name'), str)
                and isinstance(a.get('size'), int)):
            out[a['name']] = a['size']
    return out


def _report(gaps: list[str], *, subject: str,
            undersized: list[str] | None = None) -> int:
    """Print the verdict. A present-but-hollow asset is INCOMPLETE, not COMPLETE.

    Size failures share the INCOMPLETE exit code with missing ones on purpose:
    both callers already do the right thing with it (the version gate rebuilds,
    the retarget step moves an unpublished tag), and a hollow asset needs
    exactly the same treatment as an absent one — rebuild and replace.
    """
    undersized = undersized or []
    if gaps or undersized:
        if gaps:
            print(f'INCOMPLETE — {subject} is missing '
                  f'{len(gaps)} required asset(s):')
            for g in gaps:
                print(f'  - {g}')
        if undersized:
            print(f'INCOMPLETE — {subject} has '
                  f'{len(undersized)} implausibly small asset(s):')
            for u in undersized:
                print(f'  - {u}')
        return EXIT_INCOMPLETE
    print(f'COMPLETE — {subject} has every required asset at a plausible size.')
    return EXIT_COMPLETE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description='Decide whether a Tofu release is complete.')
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--dir', metavar='PATH',
                     help='directory of downloaded build artifacts (local gate)')
    src.add_argument('--release-json', metavar='FILE',
                     help='GitHub release payload, or - for stdin (remote gate)')
    ap.add_argument('--require-checksums', action='store_true',
                    help=f'also require {CHECKSUMS_ASSET} (published releases)')
    args = ap.parse_args(argv)

    if args.dir:
        root = Path(args.dir)
        if not root.is_dir():
            print(f'UNDETERMINED — no such directory: {root}')
            return EXIT_UNDETERMINED
        # rglob: download-artifact can nest per-artifact subdirectories.
        paths = [p for p in root.rglob('*') if p.is_file()]
        names = [p.name for p in paths]
        sizes = {p.name: p.stat().st_size for p in paths}
        return _report(
            missing_assets(names, require_checksums=args.require_checksums),
            subject=f'{root}',
            undersized=undersized_assets(sizes))

    text = (sys.stdin.read() if args.release_json == '-'
            else Path(args.release_json).read_text(encoding='utf-8', errors='replace'))
    names = names_from_release_json(text)
    if names is None:
        print('UNDETERMINED — response body is not a GitHub release object '
              '(truncated, an error page, or an API shape change).')
        return EXIT_UNDETERMINED
    return _report(
        missing_assets(names, require_checksums=args.require_checksums),
        subject='the published release',
        undersized=undersized_assets(sizes_from_release_json(text) or {}))


if __name__ == '__main__':
    raise SystemExit(main())
