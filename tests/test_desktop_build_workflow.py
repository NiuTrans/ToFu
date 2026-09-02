"""tests/test_desktop_build_workflow.py — the desktop installer must actually
get BUILT and PUBLISHED, and its runner labels must not be allowed to rot.

WHAT THIS GUARDS
----------------
Two independent defects made "push a new version" stop producing an installer.
Both were SILENT: nothing failed, nothing was logged, and the Releases page
simply kept showing an old version while the operator built by hand.

  1. **The build only fired on a tag that a plain push never creates.**
     ``build-desktop.yml`` triggered on ``refs/tags/v*`` only. But
     ``export.py --push`` creates a tag ONLY when ``--bump`` is also passed
     (``is_release=bool(args.bump)``; see ``_git_push``). So the normal
     "export and push" produced no tag, hence no run, hence no installer —
     the automation depended on the operator remembering a flag.

  2. **A retired runner label starves instead of failing.** The matrix asked
     for ``macos-13`` (Intel). GitHub retired that image on 2025-12-08
     (actions/runner-images#13046). A retired label does not error — it is
     never scheduled. Measured on the real repo, runs 29632725079 (v0.15.0),
     29927622183 (v0.15.1) and 30001088220 (v0.15.2): the ``macOS DMG
     (x86_64)`` job sat queued with ``runner=""`` for EXACTLY 24 h, GitHub
     auto-cancelled it, and since ``release`` has ``needs: build-macos`` the
     release job went ``skipped``. Those versions built 3 of 4 platforms and
     published nothing. ``timeout-minutes: 30`` did not help: it measures
     execution time, not queue time.

  3. **A tag is a PRODUCT of releasing, not evidence of it.** The first fix
     for (1) gated on ``git ls-remote --tags``, which is wrong in both
     directions and left the reported symptom in place — see
     ``test_an_orphan_tag_does_not_block_the_release``. Worse, an orphan tag
     also mis-anchors the release itself: ``target_commitish`` is documented
     as "Unused if the Git tag already exists", so a release created on a
     stale tag ships binaries from one commit and Source-code archives from
     another. See ``test_an_orphan_tag_is_moved_to_the_built_commit``.

THE INVARIANTS PINNED HERE
--------------------------
  * The version in ``VERSION`` — not a flag the operator may forget — is what
    decides whether an installer is built and published.
  * Every ``runs-on`` label is CURRENT, and this test goes red BEFORE the
    label's published retirement date, so the rotation happens on a normal
    working day instead of being discovered as a non-release weeks later.
  * The release job cannot be reached while any platform leg is missing.

WHY A DATED ALLOWLIST RATHER THAN "IS THIS LABEL VALID"
-------------------------------------------------------
Label validity is only observable at GitHub's scheduler, which the test suite
cannot reach (and must not depend on — CI runs offline-ish and must be
deterministic). A retirement date, however, is published months ahead. Pinning
the dates locally converts "we find out when a release silently doesn't
happen" into "a test fails while there is still time to rotate".
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest

yaml = pytest.importorskip('yaml', reason='PyYAML required to parse workflows')

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / '.github' / 'workflows' / 'build-desktop.yml'

# ── GitHub-hosted runner labels, with the date each becomes unusable ──
#
# Sources (checked 2026-07-29):
#   macos-13        retired 2025-12-08  actions/runner-images#13046
#   macos-14        deprecation began 2026-07-06, unsupported 2026-11-02
#                                       actions/runner-images#13518
#   macos-15-intel  available until 2027-08 — the LAST x86_64 macOS image
#                                       actions/runner-images#13045
#
# `None` = no announced end-of-life. When GitHub announces one, put the date
# here; this test will then start failing 90 days ahead of it.
_RUNNER_EOL: dict[str, _dt.date | None] = {
    'ubuntu-latest':   None,
    'ubuntu-24.04':    None,
    'ubuntu-22.04':    None,
    'windows-latest':  None,
    'windows-2025':    None,
    'windows-2022':    None,
    'macos-latest':    None,
    'macos-15':        None,
    'macos-15-intel':  _dt.date(2027, 8, 1),
    # ── Known-dead: present so a revert to them names the reason ──
    'macos-13':        _dt.date(2025, 12, 8),
    'macos-14':        _dt.date(2026, 11, 2),
}

# Rotate this far ahead of the published end-of-life. Long enough that the
# replacement image exists and has been exercised by the ecosystem; short
# enough that the warning is actionable rather than background noise.
_ROTATE_LEAD_DAYS = 90


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding='utf-8'))


def _triggers(wf: dict) -> dict:
    """Return the ``on:`` block.

    YAML 1.1 parses a bare ``on`` key as the boolean ``True``, so the block
    lands under ``True`` rather than ``'on'`` — read both spellings or this
    helper silently returns ``{}`` and every trigger assertion vacuously
    passes.
    """
    return wf.get('on') or wf.get(True) or {}


def _runner_labels(wf: dict) -> dict[str, list[str]]:
    """Map job name → the concrete runner labels it can land on."""
    out: dict[str, list[str]] = {}
    for job_name, job in (wf.get('jobs') or {}).items():
        runs_on = job.get('runs-on')
        labels: list[str] = []
        if isinstance(runs_on, str) and '${{' in runs_on:
            # Matrix-driven: resolve through strategy.matrix.include.
            key = re.search(r'matrix\.(\w+)', runs_on)
            include = (job.get('strategy') or {}).get('matrix', {}).get('include') or []
            if key:
                labels = [e[key.group(1)] for e in include if key.group(1) in e]
        elif isinstance(runs_on, str):
            labels = [runs_on]
        elif isinstance(runs_on, list):
            labels = [x for x in runs_on if isinstance(x, str)]
        if labels:
            out[job_name] = labels
    return out


# ══════════════════════════════════════════════════════════════════
#  Runner labels must be current
# ══════════════════════════════════════════════════════════════════

def test_every_runner_label_is_known():
    """An unrecognised label cannot be checked for retirement.

    Silently accepting one would restore exactly the blind spot this file
    exists to close, so a new label must be added to ``_RUNNER_EOL`` (with its
    published end-of-life, if any) as part of the change that introduces it.
    """
    unknown = {
        job: [lbl for lbl in labels if lbl not in _RUNNER_EOL]
        for job, labels in _runner_labels(_workflow()).items()
    }
    unknown = {k: v for k, v in unknown.items() if v}
    assert not unknown, (
        f'Unknown runner label(s): {unknown}. Add each to _RUNNER_EOL in this '
        f'test with its published end-of-life date (or None), so retirement '
        f'can be caught before it starves a release.'
    )


def test_no_job_runs_on_a_retired_or_soon_retired_runner():
    """THE REGRESSION. ``macos-13`` here is what skipped three releases.

    Fails once a label is within ``_ROTATE_LEAD_DAYS`` of its end-of-life —
    deliberately BEFORE the label stops being scheduled, because after that
    point the symptom is a 24-hour queue and a skipped release, not an error.
    """
    today = _dt.date.today()
    doomed: list[str] = []
    for job, labels in _runner_labels(_workflow()).items():
        for lbl in labels:
            eol = _RUNNER_EOL.get(lbl)
            if eol is None:
                continue
            days = (eol - today).days
            if days <= _ROTATE_LEAD_DAYS:
                state = 'RETIRED' if days <= 0 else f'retires in {days}d'
                doomed.append(f'{job} → {lbl} ({state}, EOL {eol})')
    assert not doomed, (
        'Runner label(s) at/near end-of-life:\n  ' + '\n  '.join(doomed) +
        '\n\nA retired label is NOT an error on GitHub — the job is simply '
        'never scheduled, queues for 24 h, gets auto-cancelled, and the '
        'dependent release job is SKIPPED. Rotate to a current label.'
    )


def test_both_macos_architectures_are_still_built():
    """Intel + Apple Silicon, or Intel Macs cannot install Tofu at all.

    The arch NAMES (used in the asset filenames the release gate asserts on)
    are pinned here; the runner labels behind them are free to rotate.
    """
    wf = _workflow()
    include = (wf['jobs']['build-macos']['strategy']['matrix']['include'])
    assert {e['arch'] for e in include} == {'arm64', 'x86_64'}, (
        f'macOS matrix must build both arches, got {include}'
    )


# ══════════════════════════════════════════════════════════════════
#  A push must be able to produce a release on its own
# ══════════════════════════════════════════════════════════════════

def test_a_plain_push_to_main_can_trigger_the_build():
    """THE REGRESSION. Tag-only triggering meant `--push` alone built nothing.

    ``export.py`` sets ``is_release=bool(args.bump)`` and only tags on a
    release, so requiring a tag made every non-bumped push a no-op.
    """
    push = _triggers(_workflow()).get('push') or {}
    assert 'main' in (push.get('branches') or []), (
        f'build-desktop must trigger on pushes to main; got push={push!r}. '
        f'Triggering on tags alone means `export.py --push` (no --bump) '
        f'creates no tag and therefore never builds an installer.'
    )


def test_the_version_file_is_what_decides_a_release():
    """VERSION is the single source of truth — not an operator-remembered flag."""
    ver_job = _workflow()['jobs']['version']
    body = _version_gate_script()
    assert 'cat VERSION' in body, 'version job must read the VERSION file'
    assert 'should_release' in ver_job.get('outputs', {}), (
        'version job must publish a should_release output'
    )


def _version_gate_script() -> str:
    """The ``version`` job's release-probe shell, resolved by step ``id``.

    Deliberately NOT ``steps[-1]``. That positional shorthand encoded "the
    probe is the last step of the job", which was true only until a second
    step was appended after it — adding the CHANGELOG gate broke six tests at
    once, none of which were about step ordering. The step carries ``id: ver``
    (the job's own outputs already reference ``steps.ver.outputs.*``), so that
    id is the stable handle the workflow itself relies on.
    """
    steps = _workflow()['jobs']['version']['steps']
    for s in steps:
        if s.get('id') == 'ver':
            return s['run']
    raise AssertionError(
        "the version job has no step with id 'ver' — the job's own outputs "
        'reference steps.ver.outputs.*, so that id must exist. Steps: '
        f'{[s.get("name") or s.get("id") or s.get("uses") for s in steps]}')


def _run_version_gate(*, http_code: str | None, event: str = 'push',
                      version: str = '0.15.2') -> dict[str, str]:
    """Execute the REAL ``version`` job body with a stubbed release probe.

    The gate's whole job is to turn an HTTP status into a build/skip verdict,
    so asserting on the SCRIPT TEXT cannot tell a correct gate from a broken
    one — that is precisely how the tag-based predicate survived review. This
    runs the shipped shell with a ``curl`` stub earlier on PATH, and reads the
    ``should_release`` it actually writes to ``$GITHUB_OUTPUT``.

    Args:
        http_code: what the stubbed probe prints as the status. ``None``
            simulates a transport failure (curl exits non-zero, prints
            nothing) — the case a naive ``set -e`` script would die on.
        event: value of ``GITHUB_EVENT_NAME``.
        version: contents of the VERSION file.

    Returns:
        The parsed ``key=value`` pairs the step wrote to ``$GITHUB_OUTPUT``.
    """
    body = _version_gate_script()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / 'VERSION').write_text(version + '\n', encoding='utf-8')
        out = tmp / 'gh_output'
        out.touch()

        bind = tmp / 'bin'
        bind.mkdir()
        if http_code is None:
            # Transport failure: no stdout, non-zero exit.
            stub = '#!/bin/sh\nexit 7\n'
        else:
            stub = f'#!/bin/sh\nprintf %s {http_code}\n'
        (bind / 'curl').write_text(stub, encoding='utf-8')
        (bind / 'curl').chmod(0o755)

        # `git` is stubbed to report that the TAG EXISTS (ls-remote exits 0).
        # This is not decoration: it reproduces the live state of this repo,
        # where v0.15.0-v0.15.2 are all tagged on the remote. Without it a
        # tag-based predicate would fail `ls-remote` here (no origin in a temp
        # dir) and fall through to "build" by accident — which would let the
        # broken predicate pass the orphan-tag test and make the whole NEUTER
        # vacuous. With it, asking about tags gives the WRONG answer and the
        # guard bites.
        (bind / 'git').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        (bind / 'git').chmod(0o755)

        env = {
            **os.environ,
            'PATH': f'{bind}{os.pathsep}' + os.environ.get('PATH', ''),
            'GITHUB_OUTPUT': str(out),
            'GITHUB_EVENT_NAME': event,
            'GITHUB_REPOSITORY': 'rangehow/ToFu',
            'GITHUB_API_URL': 'https://api.github.com',
            'GH_TOKEN': 'stub-token',
            'VERSION_OVERRIDE': '',
        }
        proc = subprocess.run(['bash', '-c', body], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f'version gate exited {proc.returncode}\n'
            f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
        )
        parsed: dict[str, str] = {}
        for line in out.read_text(encoding='utf-8').splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                parsed[k] = v
        return parsed


def test_an_orphan_tag_does_not_block_the_release():
    """THE REGRESSION THIS FILE PREVIOUSLY GUARDED THE WRONG SIDE OF.

    The gate used to ask ``git ls-remote --tags`` — "does the tag exist?" — as
    a proxy for "was this version released?". Measured on the real repo on
    2026-07-29, those two answers disagree for every version that matters:
    v0.15.0 and v0.15.2 are both TAGGED on the remote and both return HTTP 404
    from ``GET /releases/tags/{tag}`` — tagged, never released, because the
    starved macOS leg skipped the release job.

    Under the tag predicate the current VERSION (0.15.2) resolved to
    ``should_release=false``: the versions the user reported as missing could
    never be published, by construction. The predicate must ask about the
    Release.
    """
    got = _run_version_gate(http_code='404')
    assert got.get('should_release') == 'true', (
        f'A tagged-but-unreleased version must still build; got {got!r}. '
        f'Asking "does the tag exist?" instead of "was it released?" strands '
        f'every version whose release job was skipped — exactly the state '
        f'v0.15.0 and v0.15.2 are in.'
    )
    assert got.get('version') == '0.15.2'


def test_an_already_released_version_does_not_rebuild():
    """Complement: a version with a real, COMPLETE Release must NOT spin the
    runners.

    Without this, "always build" would satisfy the test above, and every
    content push to main would burn four runners (~30 min each, macOS at the
    highest rate) racing to republish a shipped release.

    RE-ANCHORED (partial-release fix): this used to pass a bare ``200``,
    because at the time the status code WAS the whole verdict. It no longer
    is — a release object can exist with zero assets, and treating that as
    "shipped" is the defect ``test_a_partial_release_is_rebuilt`` covers. So
    the fixture now has to supply a complete asset list, which is what "already
    released" actually means. The invariant this test protects (a genuinely
    shipped version must not rebuild) is unchanged; only the evidence required
    to establish "shipped" got stricter.
    """
    got, log = _run_version_gate_body(
        http_code='200', body_text=_release_json(_complete_names()))
    assert got.get('should_release') == 'false', (
        f'a complete Release means the version shipped — the gate must skip; '
        f'got {got!r}\nlog:\n{log}'
    )


@pytest.mark.parametrize('code', ['403', '429', '500', '502', None])
def test_an_unreadable_probe_builds_rather_than_skipping(code):
    """Failure direction is asymmetric ON PURPOSE.

    Rate limit, auth hiccup, 5xx, or a transport failure must all resolve
    toward BUILDING. A redundant build costs four runners; a skipped one is a
    silent non-release — the original defect. ``None`` is the transport-failure
    case that also proves ``set -e`` does not abort the step.
    """
    got = _run_version_gate(http_code=code)
    assert got.get('should_release') == 'true', (
        f'probe HTTP {code!r} must fail OPEN (build), got {got!r}. '
        f'Uncertainty resolving toward "skip" reproduces the silent '
        f'non-release this workflow exists to prevent.'
    )


def test_manual_dispatch_always_builds():
    """The rebuild/re-release escape hatch must not consult the probe at all."""
    got = _run_version_gate(http_code='200', event='workflow_dispatch')
    assert got.get('should_release') == 'true', (
        f'workflow_dispatch is explicit human intent and must always build, '
        f'even when a release already exists; got {got!r}'
    )


def test_the_gate_asks_the_release_api_not_the_tag_list():
    """Pin the SHAPE of the question, not just today's verdicts.

    A gate that happened to return the right answers while still consulting
    tags would pass the behavioural tests above only by luck of the stub.

    Comments are stripped before asserting: this step's own comment block
    NAMES the rejected approaches (``ls-remote``, ``gh release view``) to
    explain why they are wrong, so a raw substring scan would flag the very
    documentation that prevents the regression — and, worse, could be silenced
    by deleting that explanation.
    """
    body = _version_gate_script()
    code = '\n'.join(
        line for line in body.splitlines()
        if not line.lstrip().startswith('#')
    )
    assert '/releases/tags/' in code, (
        'the gate must query GET /repos/{owner}/{repo}/releases/tags/{tag}'
    )
    assert 'ls-remote' not in code, (
        'the gate must not consult git tags: a tag exists BEFORE the release '
        '(export.py pushes branch then tag back-to-back) and can outlive a '
        'skipped release, so it answers a different question than the one '
        'being asked'
    )
    assert 'gh release view' not in code, (
        '`gh release view` exits 1 for BOTH "no such release" and "the API '
        'call failed" (cli/cli#6024, undocumented), so it cannot express the '
        'fail-open rule'
    )


def test_every_build_job_is_gated_on_should_release():
    """A gate the build jobs ignore is not a gate."""
    wf = _workflow()
    ungated = [
        name for name, job in wf['jobs'].items()
        if name != 'version'
        and 'should_release' not in str(job.get('if', ''))
    ]
    assert not ungated, (
        f'These jobs run regardless of should_release: {ungated}. Every build '
        f'and the release itself must respect the gate, or an ordinary push '
        f'burns four runners and may republish a shipped version.'
    )


def test_the_release_publishes_under_the_version_tag():
    """The run is triggered by a BRANCH push, so the release must carry its tag.

    Without an explicit ``tag_name`` the action would try to tag from the
    triggering ref (``refs/heads/main``) and the release would not be
    addressable as ``v<VERSION>`` — breaking ``/releases/latest`` deep links
    and the in-app updater's version comparison.
    """
    steps = _workflow()['jobs']['release']['steps']
    create = [s for s in steps if 'action-gh-release' in str(s.get('uses', ''))]
    assert create, 'release job must create a GitHub Release'
    with_ = create[0].get('with', {})
    assert 'tag_name' in with_ and 'version' in str(with_['tag_name']), (
        f'release must publish under the v<VERSION> tag; got {with_!r}'
    )
    assert with_.get('draft') is False, 'a draft release is invisible to users'


def test_agent_legs_build_all_three_platforms():
    """The agent component ships from the SAME three platform jobs (one
    change, legs+gates atomically — docs/modules/remote_execution.md)."""
    wf_text = _WORKFLOW.read_text(encoding='utf-8')
    # One PyInstaller agent build per platform job (windows/macos/linux).
    assert wf_text.count('pyinstaller tofu-agent.spec') == 3, (
        'each platform job must also build the agent component')
    # The upload artifact names the release job merges.
    for artifact in ('windows-agent-installer',
                     'macos-agent-dmg-${{ matrix.arch }}',
                     'linux-agent-archive'):
        assert artifact in wf_text, f'agent artifact {artifact} not uploaded'
    # Smoke on the two platforms where the full leg smokes (macOS smokes
    # neither component — same discipline as the full DMG leg).
    assert wf_text.count('TOFU_AGENT_SMOKE_OK') >= 2, (
        'windows and linux agent builds must run the smoke gate')


def test_agent_windows_inno_carries_the_autostart_contract():
    """Owner amendment ①, CI side: offered, default ON, HKCU (UAC-free),
    removed at uninstall — and the SAME value name the tray writes."""
    wf_text = _WORKFLOW.read_text(encoding='utf-8')
    assert 'AppName=Tofu Agent' in wf_text
    assert ('OutputBaseFilename=TofuAgent-Setup-${APP_VERSION}-win64'
            in wf_text)
    assert r'{localappdata}\\Programs\\TofuAgent' in wf_text
    assert r'Software\\Microsoft\\Windows\\CurrentVersion\\Run' in wf_text
    assert 'ValueName: "TofuAgent"' in wf_text, (
        'the Run value name must equal agent_launcher._RUN_VALUE')
    assert 'uninsdeletevalue' in wf_text, (
        'a removed agent must not leave a dead autorun behind')
    # Default-ON: the task exists and is never flagged unchecked.
    assert 'Name: autostart; Description: "Start Tofu Agent with Windows"' \
        in wf_text
    import re as _re
    assert not _re.search(r'autostart[^\n]*unchecked', wf_text, _re.I), (
        'the autostart task must default to CHECKED — an unattended relay '
        'that has to be ticked by hand is not unattended')


def test_required_assets_cover_both_components():
    """The gates' required set spans BOTH tables, derived — a release
    missing an agent asset is as INCOMPLETE as one missing a platform."""
    mod = _asset_mod()
    total = len(mod.PLATFORM_ASSETS) + len(mod.AGENT_PLATFORM_ASSETS)
    assert len(mod.REQUIRED_PLATFORM_ASSETS) == total + 2
    labels = [l for l, _p in mod.REQUIRED_PLATFORM_ASSETS]
    assert any('agent' in l.lower() for l in labels), (
        'the agent component is not in the release gates')


def test_release_still_requires_every_platform_leg():
    """The completeness gate must not have been loosened to route around this.

    A tempting "fix" for a starving leg is to drop it from ``needs`` — which
    would ship a partial Latest and silently reintroduce "Intel Macs can't
    install Tofu".
    """
    needs = _workflow()['jobs']['release']['needs']
    assert {'build-windows', 'build-macos', 'build-linux'} <= set(needs), (
        f'release must depend on every platform build; got {needs}'
    )


# ══════════════════════════════════════════════════════════════════
#  The release must be cut from the commit that was actually built
# ══════════════════════════════════════════════════════════════════

_BUILT_SHA = 'a' * 40
_STALE_SHA = 'b' * 40


def _retarget_step() -> dict:
    """The step that moves an orphan tag onto the built commit."""
    steps = _workflow()['jobs']['release']['steps']
    hits = [s for s in steps
            if 'refs/tags/' in str(s.get('run', ''))
            and 'push' in str(s.get('run', ''))]
    assert len(hits) == 1, (
        f'expected exactly one tag-retarget step in the release job, found '
        f'{len(hits)}. Without it, a release created on a pre-existing tag is '
        f'anchored to that tag\'s commit — target_commitish is "Unused if the '
        f'Git tag already exists" (GitHub REST docs).'
    )
    return hits[0]


def _run_retarget(*, remote_sha: str | None, http_code: str | None,
                  built_sha: str = _BUILT_SHA,
                  tag_object_sha: str | None = None) -> list[str]:
    """Execute the REAL retarget step; return the git commands it issued.

    Asserting on the step's text cannot distinguish "moves an orphan tag" from
    "moves any tag it finds" — and the difference between those two is a
    rewritten published tag. So this runs the shipped shell with ``git`` and
    ``curl`` stubs and reports what it actually tried to do.

    The ls-remote fixtures are written to FILES and ``cat``-ed by the stub.
    They must contain a real TAB: emitting them inline via Python ``repr``
    produces a literal backslash-t, ``awk '{print $1}'`` then sees one
    unsplittable field, and every SHA comparison in the step silently fails to
    match — which made this harness report a push that the real step would not
    have made.

    Args:
        remote_sha: the COMMIT the tag resolves to (the peeled ``^{}`` ref).
            ``None`` means the tag does not exist on the remote at all.
        http_code: status from the release probe. ``None`` = transport failure.
        built_sha: the commit this run built (``GITHUB_SHA``).
        tag_object_sha: for an ANNOTATED tag, the tag object's own SHA — what
            the unpeeled ref reports, which is never the commit. ``None``
            models a lightweight tag (both refs report the commit).

    Returns:
        Every ``git`` argv the step invoked, one space-joined string each.
    """
    body = _retarget_step()['run']
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        log = tmp / 'git.log'
        bind = tmp / 'bin'
        bind.mkdir()

        peeled = tmp / 'ls_peeled'
        plain = tmp / 'ls_plain'
        if remote_sha is None:
            peeled.write_text('', encoding='utf-8')
            plain.write_text('', encoding='utf-8')
        else:
            peeled.write_text(f'{remote_sha}\trefs/tags/v0.15.2^{{}}\n',
                              encoding='utf-8')
            plain.write_text(
                f'{tag_object_sha or remote_sha}\trefs/tags/v0.15.2\n',
                encoding='utf-8')

        # Logs every invocation; answers ls-remote from the fixtures, choosing
        # the peeled or unpeeled file by which ref the step asked for. Any
        # other subcommand (notably `push`) just succeeds — what matters is
        # whether it was CALLED, not whether it reached a remote.
        (bind / 'git').write_text(
            '#!/bin/sh\n'
            f'printf "%s\\n" "$*" >> {log}\n'
            'if [ "$1" = "ls-remote" ]; then\n'
            '  case "$*" in\n'
            f'    *"^{{}}"*) cat {peeled} ;;\n'
            f'    *)         cat {plain} ;;\n'
            '  esac\n'
            'fi\n'
            'exit 0\n',
            encoding='utf-8')
        (bind / 'git').chmod(0o755)

        if http_code is None:
            curl_stub = '#!/bin/sh\nexit 7\n'
        else:
            curl_stub = f'#!/bin/sh\nprintf %s {http_code}\n'
        (bind / 'curl').write_text(curl_stub, encoding='utf-8')
        (bind / 'curl').chmod(0o755)

        env = {
            **os.environ,
            'PATH': f'{bind}{os.pathsep}' + os.environ.get('PATH', ''),
            'GITHUB_SHA': built_sha,
            'GITHUB_REPOSITORY': 'rangehow/ToFu',
            'GITHUB_API_URL': 'https://api.github.com',
            'GH_TOKEN': 'stub-token',
            'VER': '0.15.2',
        }
        proc = subprocess.run(['bash', '-c', body], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f'retarget step exited {proc.returncode}\n'
            f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
        )
        if not log.exists():
            return []
        return [ln for ln in log.read_text(encoding='utf-8').splitlines() if ln]


def _pushes(cmds: list[str]) -> list[str]:
    return [c for c in cmds if c.startswith('push')]


def test_an_orphan_tag_is_moved_to_the_built_commit():
    """THE DEFECT. An adopted orphan tag anchors the release to the OLD commit.

    ``target_commitish`` is documented as "Unused if the Git tag already
    exists", and action-gh-release looks for an existing RELEASE
    (``findTagFromReleases``), not an existing git ref — so when the tag is
    present with no release behind it, the action happily creates a release
    ON THE STALE COMMIT. The installers come from ``github.sha``; the Source
    code (zip/tar.gz) and the generated notes come from wherever the orphan
    tag points. ``make_latest`` then hands that mismatch to every user, and
    nothing anywhere errors.

    Measured on the remote 2026-07-29: v0.15.0 and v0.15.2 are tagged and both
    404 from the Releases API, so this is the state the very next push lands
    in.
    """
    cmds = _run_retarget(remote_sha=_STALE_SHA, http_code='404')
    pushes = _pushes(cmds)
    assert pushes, (
        f'an orphan tag (tag present, release 404) must be moved onto the '
        f'built commit; the step issued no push at all. git calls: {cmds!r}'
    )
    assert any(_BUILT_SHA in p and 'refs/tags/v0.15.2' in p for p in pushes), (
        f'the tag must be repointed at GITHUB_SHA ({_BUILT_SHA[:8]}…); '
        f'got {pushes!r}'
    )


def test_a_published_tag_is_never_moved():
    """THE COMPLEMENT, and the one that makes this safe to do automatically.

    ``should_release`` is NOT authorisation to rewrite a tag:
    ``workflow_dispatch`` sets it to true WITHOUT probing (the deliberate
    re-release hatch), so a manual re-run on a shipped version reaches this
    step. Moving the tag there would rewrite published history and break every
    downstream pin — the exact thing ``export.py::_tag_push_action`` refuses to
    do without an explicit human ``--force``. So the step must re-probe and
    move only tags with NO release behind them.
    """
    cmds = _run_retarget(remote_sha=_STALE_SHA, http_code='200')
    assert not _pushes(cmds), (
        f'a tag with a real Release must NEVER be force-moved; the step '
        f'pushed anyway: {_pushes(cmds)!r}'
    )


def test_an_unreadable_probe_does_not_move_the_tag():
    """Here uncertainty must resolve toward NOT touching the tag.

    This is deliberately the opposite of the version gate's fail-open: a
    redundant build is cheap and reversible, whereas force-moving a tag that
    turns out to have been published is destructive and hard to undo. So an
    ambiguous probe declines and warns.
    """
    for code in ('403', '429', '500', None):
        cmds = _run_retarget(remote_sha=_STALE_SHA, http_code=code)
        assert not _pushes(cmds), (
            f'probe HTTP {code!r} is not proof the tag is an orphan — the step '
            f'must not move it, but it pushed: {_pushes(cmds)!r}'
        )


def test_a_tag_already_on_the_built_commit_is_left_alone():
    """The `--bump` path: export.py just pushed this exact tag. No-op, no force."""
    cmds = _run_retarget(remote_sha=_BUILT_SHA, http_code='404')
    assert not _pushes(cmds), (
        f'tag already points at the built commit — nothing to do, but the '
        f'step pushed: {_pushes(cmds)!r}'
    )


def test_an_annotated_tag_is_compared_by_its_peeled_commit():
    """``export.py`` creates ANNOTATED tags (``git tag -a``), and those do not
    report a commit from the plain ref — they report the tag OBJECT's sha.

    Comparing that object sha against ``GITHUB_SHA`` never matches, so a
    correct-but-naive step would force-push on the `--bump` path every single
    time: rewriting a tag that export.py had just placed correctly, on the one
    path where it was already right. The step must peel with ``^{}`` first.
    """
    cmds = _run_retarget(remote_sha=_BUILT_SHA, http_code='404',
                         tag_object_sha='c' * 40)
    assert not _pushes(cmds), (
        f'an annotated tag whose PEELED commit is already the built commit '
        f'must be left alone; the step compared the tag object instead and '
        f'pushed: {_pushes(cmds)!r}'
    )


def test_a_missing_tag_is_left_for_the_release_action_to_create():
    """No tag yet: action-gh-release creates it, and target_commitish DOES apply."""
    cmds = _run_retarget(remote_sha=None, http_code='404')
    assert not _pushes(cmds), (
        f'no tag exists yet, so there is nothing to move — the release action '
        f'creates it from target_commitish; step pushed: {_pushes(cmds)!r}'
    )


def test_the_retarget_runs_before_the_release_is_created():
    """Order is the whole point: after the release, the anchor is already wrong."""
    steps = _workflow()['jobs']['release']['steps']
    retarget = next(i for i, s in enumerate(steps)
                    if 'refs/tags/' in str(s.get('run', ''))
                    and 'push' in str(s.get('run', '')))
    create = next(i for i, s in enumerate(steps)
                  if 'action-gh-release' in str(s.get('uses', '')))
    assert retarget < create, (
        f'the tag must be repointed BEFORE the release is created (retarget at '
        f'step {retarget}, release at step {create}). Afterwards the release is '
        f'already anchored to the stale commit and moving the tag does not '
        f're-cut its Source code archives.'
    )


def test_the_retarget_uses_the_built_sha_not_a_branch_name():
    """``main`` moves; the built commit does not.

    Sibling sessions push to this repo continuously, so by the time the
    release job runs, ``main`` may already be several commits past what the
    installers were built from. Anchoring the tag to the branch would
    reintroduce the same source/binary mismatch this step exists to prevent.
    """
    body = _retarget_step()['run']
    code = '\n'.join(ln for ln in body.splitlines()
                     if not ln.lstrip().startswith('#'))
    assert 'GITHUB_SHA' in code, (
        'the retarget must use GITHUB_SHA (the exact commit the artifacts were '
        'built from), not a branch ref that keeps moving'
    )
    assert not re.search(r'push\s+--force\s+origin\s+["\']?(main|HEAD)\b', code), (
        'the retarget must not point the tag at a moving branch'
    )


# ══════════════════════════════════════════════════════════════════
#  Two runs must never race to publish the same release
# ══════════════════════════════════════════════════════════════════

def _resolve_expr(template: str, ctx: dict) -> str:
    """Evaluate a workflow ``${{ ... }}`` template against a fake context.

    Only the dotted-lookup form this workflow actually uses is supported.
    Anything else raises rather than silently resolving to a constant --
    a resolver that quietly returns the same string for every input would
    make the mutual-exclusion assertions below pass no matter what the
    group key is, which is the specific way this kind of guard goes hollow.
    """
    def sub(m):
        expr = m.group(1).strip()
        cur = ctx
        for part in expr.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                raise AssertionError(
                    f'concurrency group uses {expr!r}, which this test cannot '
                    f'evaluate. Extend _resolve_expr (and think about whether '
                    f'the new context varies per RUN -- if it does, the group '
                    f'no longer serialises anything).'
                )
            cur = cur[part]
        return str(cur)
    return re.sub(r'\$\{\{([^}]*)\}\}', sub, template)


def _push_ctx(*, ref: str, sha: str, run_id: str) -> dict:
    return {'github': {
        'workflow': 'Build Desktop', 'ref': ref, 'ref_name': ref.split('/')[-1],
        'sha': sha, 'run_id': run_id, 'run_number': run_id,
        'repository': 'rangehow/ToFu', 'event_name': 'push',
    }}


def test_two_pushes_to_the_same_branch_cannot_both_reach_the_release():
    """THE HAZARD THE BRANCH TRIGGER CREATED.

    Measured on this repo (2026-07-28 onward, 339 commits): median gap between
    commits is 165 s, while a build leg may take 30 min. So while v<VERSION> is
    unreleased, many pushes land inside one build window. Each run's release
    job would independently probe the tag, see 404, force-move v<VERSION> to
    ITS OWN github.sha, and then publish -- last writer wins the ref while an
    earlier run may already have uploaded assets. That is the same
    binaries-from-one-commit / source-from-another mismatch the retarget step
    exists to prevent, arriving by a different road.

    The invariant is therefore about the RESOURCE, not about a YAML key: two
    runs of this workflow on the same branch must land in one concurrency
    group, so at most one of them can be executing at a time.
    """
    wf = _workflow()
    conc = wf.get('concurrency')
    assert conc, (
        'build-desktop must declare a concurrency group. Without one, every '
        'push into the unreleased window starts a full four-platform build, '
        'and their release jobs race to force-move the same tag.'
    )
    group = conc['group'] if isinstance(conc, dict) else conc

    a = _resolve_expr(group, _push_ctx(
        ref='refs/heads/main', sha='a' * 40, run_id='1001'))
    b = _resolve_expr(group, _push_ctx(
        ref='refs/heads/main', sha='b' * 40, run_id='1002'))
    assert a == b, (
        f'two pushes to main resolved to DIFFERENT concurrency groups '
        f'({a!r} vs {b!r}), so both would run and both would force-move the '
        f'same tag. The group must not vary per run -- keys like github.sha '
        f'or github.run_id defeat the whole mechanism.'
    )


def test_the_group_does_not_serialise_unrelated_branches():
    """Complement: the group must not be a single global constant.

    Only the positive assertion above is satisfiable by `group: "x"`, which would
    make every branch (and any future release line) queue behind main for up
    to 30 minutes per build. The contended resource is the PER-BRANCH release,
    so the key has to carry the ref.
    """
    conc = _workflow().get('concurrency')
    assert conc, (
        'build-desktop must declare a concurrency group -- see '
        'test_two_pushes_to_the_same_branch_cannot_both_reach_the_release '
        'for why a missing group lets two runs force-move the same tag.'
    )
    group = conc['group'] if isinstance(conc, dict) else conc
    main = _resolve_expr(group, _push_ctx(
        ref='refs/heads/main', sha='a' * 40, run_id='1'))
    other = _resolve_expr(group, _push_ctx(
        ref='refs/heads/release-2.x', sha='c' * 40, run_id='2'))
    assert main != other, (
        f'unrelated branches share concurrency group {main!r}; they do not '
        f'contend for the same tag, so serialising them only adds latency'
    )


def test_an_in_flight_release_is_never_cancelled_by_a_newer_push():
    """cancel-in-progress must be FALSE here, against the usual CI default.

    Two independent reasons, both measured rather than assumed:

    1. A build leg runs up to 30 min (`timeout-minutes: 30`), but only 8 of
       338 measured inter-commit gaps (2.4%) are >= 30 min. With cancellation
       a build would be superseded before finishing on ~97.6% of pushes, so an
       installer would ship only during a rare quiet spell -- the very symptom
       ("version changed, no build appeared") this workflow exists to fix.

    2. Cancellation can land between the tag force-push and the asset upload,
       leaving a Release that the version gate then reads as 200 and never
       retries.

    Queueing gives the same mutual exclusion without either hazard: with the
    default `queue: single`, a burst collapses to one running plus the newest
    pending run, whose version job then probes 200 and skips in seconds.
    """
    conc = _workflow().get('concurrency')
    assert isinstance(conc, dict), (
        f'build-desktop must declare a concurrency mapping so this value can '
        f'be an explicit, reviewed choice; got {conc!r}'
    )
    assert conc.get('cancel-in-progress') is False, (
        f'cancel-in-progress must be explicitly false, got '
        f'{conc.get("cancel-in-progress")!r}. A newer push must not kill a '
        f'build that is already producing the release -- at this repo\'s push '
        f'cadence that would cancel ~97.6% of builds before they finish.'
    )
    assert conc.get('queue') != 'max', (
        'queue: max would let up to 100 redundant runs pile up; the default '
        'single-slot queue is what collapses a push burst to one build'
    )


# ══════════════════════════════════════════════════════════════════
#  A release that EXISTS is not necessarily a release that SHIPPED
# ══════════════════════════════════════════════════════════════════
#
# THE DEFECT. `action-gh-release` creates the release object first and uploads
# assets afterwards. Anything that interrupts the gap — a cancelled run, a dead
# runner, a network blip, an internal failure in the action — leaves a release
# that is addressable, tagged, and EMPTY.
#
# The version gate used to read only the HTTP status, so from then on it saw
# 200 and skipped forever: that version could never rebuild without a human
# deleting the release or bumping VERSION. No self-heal path existed.
#
# This is the 5114cbca defect one level down ("a tag is a PRODUCT of releasing,
# not evidence of it"): the gate was upgraded from asking about the tag to
# asking about the Release, which is the right direction but still a proxy for
# the thing a user needs — "can I download an installer for my platform?".

import importlib.util as _ilu

_ASSET_SCRIPT = _ROOT / 'scripts' / 'release_assets.py'


def _asset_mod():
    """Import scripts/release_assets.py (not a package, so load by path)."""
    spec = _ilu.spec_from_file_location('_tofu_release_assets', _ASSET_SCRIPT)
    assert spec and spec.loader, f'cannot load {_ASSET_SCRIPT}'
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _complete_names():
    """A full asset set, built FROM the shared list rather than hand-typed.

    Hand-typing them here would create exactly the second copy this epic
    exists to forbid: the fixture would keep passing after a platform was
    added to the script, so the new platform would go unguarded.
    """
    mod = _asset_mod()
    out = []
    for _label, pat in mod.REQUIRED_PLATFORM_ASSETS:
        out.append(pat.replace('*', '0.15.2'))
    out.append(mod.CHECKSUMS_ASSET)
    return out


def _release_json(names):
    import json as _json
    return _json.dumps({'tag_name': 'v0.15.2',
                        'assets': [{'name': n} for n in names]})


# ── the shared predicate itself ───────────────────────────────────

def test_the_asset_list_lives_in_exactly_one_place():
    """The expected-asset globs must not be inlined into the workflow.

    Two copies would let a platform be added to one gate and silently missed
    by the other — and both copies would keep passing on the platforms they
    still know about, so the drift is invisible until someone's OS has no
    installer.
    """
    mod = _asset_mod()
    wf_text = _WORKFLOW.read_text(encoding='utf-8')
    code = '\n'.join(ln for ln in wf_text.splitlines()
                     if not ln.lstrip().startswith('#'))
    leaked = [pat for _l, pat in mod.REQUIRED_PLATFORM_ASSETS if pat in code]
    assert not leaked, (
        f'These asset globs are written into the workflow as well as into '
        f'scripts/release_assets.py: {leaked}. Keep ONE list — the workflow '
        f'must call the script instead.'
    )


def test_an_empty_release_is_incomplete():
    """The exact partial-release state: release object exists, zero assets."""
    mod = _asset_mod()
    gaps = mod.missing_assets([], require_checksums=True)
    assert len(gaps) == len(mod.REQUIRED_PLATFORM_ASSETS) + 1, (
        f'an empty release must be missing every required asset, got {gaps!r}'
    )


def test_a_full_release_is_complete():
    """Complement — without it, "always incomplete" would satisfy the rest."""
    mod = _asset_mod()
    gaps = mod.missing_assets(_complete_names(), require_checksums=True)
    assert gaps == [], f'a full asset set must be complete, got missing {gaps!r}'


@pytest.mark.parametrize('drop', range(len(_complete_names())))
def test_dropping_any_single_platform_is_detected(drop):
    """Every platform is load-bearing, not just the one someone tested."""
    mod = _asset_mod()
    names = _complete_names()
    victim = names[drop]
    remaining = [n for n in names if n != victim]
    gaps = mod.missing_assets(remaining, require_checksums=True)
    assert gaps, f'dropping {victim!r} must be detected; got no missing assets'


def test_checksums_are_required_only_for_published_releases():
    """SHA256SUMS is generated AFTER the local gate runs, so it cannot be
    required there — but a published release without it is incomplete, because
    users are told to verify downloads with `shasum -a 256 -c SHA256SUMS`."""
    mod = _asset_mod()
    platforms_only = [n for n in _complete_names() if n != mod.CHECKSUMS_ASSET]
    assert mod.missing_assets(platforms_only, require_checksums=False) == [], (
        'the local gate must pass on platform assets alone'
    )
    assert mod.missing_assets(platforms_only, require_checksums=True) != [], (
        'a PUBLISHED release with no SHA256SUMS must be judged incomplete'
    )


def test_a_non_release_body_is_undetermined_not_empty():
    """A truncated body or an error page must NOT read as "zero assets".

    Treating it as empty would turn a transient API hiccup into a confident
    "this release is broken" — and on the retarget path that verdict
    force-moves a tag that may well be published.
    """
    mod = _asset_mod()
    for body in ('', 'not json', '{"message": "Not Found"}', '[]', 'null'):
        assert mod.names_from_release_json(body) is None, (
            f'{body!r} is not a release object and must be UNDETERMINED'
        )
    assert mod.names_from_release_json(_release_json([])) == [], (
        'a real release object with an empty assets array IS readable, and '
        'means zero assets — that is the partial-release case, not an error'
    )


# ── the version gate, driven end-to-end ───────────────────────────

def _run_version_gate_body(*, http_code, body_text, event='push',
                           version='0.15.2'):
    """Run the REAL version-gate shell with a curl stub that serves a BODY.

    The gate now has to read the asset list, so the older helper (which only
    stubs a status code) cannot exercise this path at all.
    """
    gate = _version_gate_script()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / 'VERSION').write_text(version + '\n', encoding='utf-8')
        out = tmp / 'gh_output'
        out.touch()

        # The gate calls `python3 scripts/release_assets.py`, resolved relative
        # to the checkout — so the REAL script must be reachable from cwd.
        (tmp / 'scripts').mkdir()
        (tmp / 'scripts' / 'release_assets.py').write_text(
            _ASSET_SCRIPT.read_text(encoding='utf-8'), encoding='utf-8')

        bind = tmp / 'bin'
        bind.mkdir()
        payload = tmp / 'payload.json'
        payload.write_text(body_text or '', encoding='utf-8')
        if http_code is None:
            stub = '#!/bin/sh\nexit 7\n'
        else:
            # Honour curl's `-o <file>`: the gate writes the body there and
            # then feeds that file to the script.
            stub = (
                '#!/bin/sh\n'
                'dest=""\n'
                'while [ $# -gt 0 ]; do\n'
                '  case "$1" in -o) dest="$2"; shift 2 ;; *) shift ;; esac\n'
                'done\n'
                f'[ -n "$dest" ] && cat {payload} > "$dest"\n'
                f'printf %s {http_code}\n'
            )
        (bind / 'curl').write_text(stub, encoding='utf-8')
        (bind / 'curl').chmod(0o755)
        (bind / 'git').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        (bind / 'git').chmod(0o755)

        env = {
            **os.environ,
            'PATH': f'{bind}{os.pathsep}' + os.environ.get('PATH', ''),
            'GITHUB_OUTPUT': str(out),
            'GITHUB_EVENT_NAME': event,
            'GITHUB_REPOSITORY': 'rangehow/ToFu',
            'GITHUB_API_URL': 'https://api.github.com',
            'GH_TOKEN': 'stub-token',
            'VERSION_OVERRIDE': '',
        }
        proc = subprocess.run(['bash', '-c', gate], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f'version gate exited {proc.returncode}\n'
            f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
        )
        parsed = {}
        for line in out.read_text(encoding='utf-8').splitlines():
            if '=' in line:
                k, _, v = line.partition('=')
                parsed[k] = v
        return parsed, proc.stdout


def test_a_partial_release_is_rebuilt():
    """THE HEADLINE. 200 + a missing DMG must still build.

    Without this the version is stranded forever: the gate reads 200, skips,
    and the missing installer never appears.
    """
    mod = _asset_mod()
    names = [n for n in _complete_names()
             if not n.endswith('macos-x86_64.dmg')]
    got, log = _run_version_gate_body(http_code='200',
                                      body_text=_release_json(names))
    assert got.get('should_release') == 'true', (
        f'a release missing the Intel DMG must be rebuilt to repair it; got '
        f'{got!r}\nlog:\n{log}'
    )


def test_an_empty_release_object_is_rebuilt():
    """The literal interrupted-upload state: release created, nothing uploaded."""
    got, log = _run_version_gate_body(http_code='200',
                                      body_text=_release_json([]))
    assert got.get('should_release') == 'true', (
        f'an empty release must be rebuilt, got {got!r}\nlog:\n{log}'
    )


def test_a_complete_release_still_skips():
    """THE COMPLEMENT that keeps this from becoming "always build".

    Without it, the fix above would be satisfied by rebuilding on every push —
    burning four runners per content push and republishing a shipped release.
    """
    got, log = _run_version_gate_body(http_code='200',
                                      body_text=_release_json(_complete_names()))
    assert got.get('should_release') == 'false', (
        f'a complete release must still skip the build; got {got!r}\n'
        f'log:\n{log}'
    )


def test_an_unreadable_asset_list_builds():
    """200 with an unparsable body resolves toward building, like every other
    uncertain outcome in this gate."""
    got, log = _run_version_gate_body(http_code='200', body_text='<html>502</html>')
    assert got.get('should_release') == 'true', (
        f'an unreadable asset list must fail open toward building; got {got!r}\n'
        f'log:\n{log}'
    )


# ── the retarget step on the repair path ──────────────────────────

def _run_retarget_body(*, remote_sha, http_code, body_text,
                       built_sha=_BUILT_SHA):
    """Run the REAL retarget step with a body-serving curl stub."""
    body = _retarget_step()['run']
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        log = tmp / 'git.log'
        bind = tmp / 'bin'
        bind.mkdir()
        (tmp / 'scripts').mkdir()
        (tmp / 'scripts' / 'release_assets.py').write_text(
            _ASSET_SCRIPT.read_text(encoding='utf-8'), encoding='utf-8')

        peeled = tmp / 'ls_peeled'
        plain = tmp / 'ls_plain'
        if remote_sha is None:
            peeled.write_text('', encoding='utf-8')
            plain.write_text('', encoding='utf-8')
        else:
            peeled.write_text(f'{remote_sha}\trefs/tags/v0.15.2^{{}}\n',
                              encoding='utf-8')
            plain.write_text(f'{remote_sha}\trefs/tags/v0.15.2\n',
                             encoding='utf-8')

        (bind / 'git').write_text(
            '#!/bin/sh\n'
            f'printf "%s\\n" "$*" >> {log}\n'
            'if [ "$1" = "ls-remote" ]; then\n'
            '  case "$*" in\n'
            f'    *"^{{}}"*) cat {peeled} ;;\n'
            f'    *)         cat {plain} ;;\n'
            '  esac\n'
            'fi\n'
            'exit 0\n', encoding='utf-8')
        (bind / 'git').chmod(0o755)

        payload = tmp / 'payload.json'
        payload.write_text(body_text or '', encoding='utf-8')
        if http_code is None:
            curl_stub = '#!/bin/sh\nexit 7\n'
        else:
            curl_stub = (
                '#!/bin/sh\n'
                'dest=""\n'
                'while [ $# -gt 0 ]; do\n'
                '  case "$1" in -o) dest="$2"; shift 2 ;; *) shift ;; esac\n'
                'done\n'
                f'[ -n "$dest" ] && cat {payload} > "$dest"\n'
                f'printf %s {http_code}\n'
            )
        (bind / 'curl').write_text(curl_stub, encoding='utf-8')
        (bind / 'curl').chmod(0o755)

        env = {
            **os.environ,
            'PATH': f'{bind}{os.pathsep}' + os.environ.get('PATH', ''),
            'GITHUB_SHA': built_sha,
            'GITHUB_REPOSITORY': 'rangehow/ToFu',
            'GITHUB_API_URL': 'https://api.github.com',
            'GH_TOKEN': 'stub-token',
            'VER': '0.15.2',
        }
        proc = subprocess.run(['bash', '-c', body], cwd=tmp, env=env,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (
            f'retarget exited {proc.returncode}\n'
            f'stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}'
        )
        cmds = ([ln for ln in log.read_text(encoding='utf-8').splitlines() if ln]
                if log.exists() else [])
        return cmds, proc.stdout


def test_a_partial_releases_tag_is_moved_to_the_built_commit():
    """THE CONSEQUENCE OF OPENING THE GATE — reachable only now.

    Before the version gate learned to rebuild partial releases, a 200 always
    meant "done" and no repair run could reach this step. Now one can: the
    release exists (200) while its tag still points at the older commit. If the
    tag stayed put, action-gh-release would update that release in place with
    installers built from GITHUB_SHA while its Source-code archives and notes
    kept coming from the stale commit — the exact binaries/source mismatch this
    step was created to prevent, arriving by the repair path instead of the
    orphan path.
    """
    names = [n for n in _complete_names() if not n.endswith('win64.exe')]
    cmds, log = _run_retarget_body(remote_sha=_STALE_SHA, http_code='200',
                                   body_text=_release_json(names))
    pushes = _pushes(cmds)
    assert any(_BUILT_SHA in p and 'refs/tags/v0.15.2' in p for p in pushes), (
        f'a PARTIAL release is being repaired by this run, so its tag must '
        f'follow the commit that produced the new assets; pushes={pushes!r}\n'
        f'log:\n{log}'
    )


def test_a_complete_releases_tag_is_still_never_moved():
    """THE COMPLEMENT, and the reason this is safe to automate.

    A genuinely published release has downstream pins. Only an INCOMPLETE one
    may be retargeted; a complete one is left exactly where it is.
    """
    cmds, log = _run_retarget_body(remote_sha=_STALE_SHA, http_code='200',
                                   body_text=_release_json(_complete_names()))
    assert not _pushes(cmds), (
        f'a COMPLETE published release must never have its tag force-moved; '
        f'pushes={_pushes(cmds)!r}\nlog:\n{log}'
    )


def test_an_unreadable_asset_list_does_not_move_the_tag():
    """Here uncertainty must resolve the OTHER way from the version gate.

    A redundant build is cheap; force-moving a tag that turns out to have been
    published is destructive and hard to undo. So "200 but I cannot read the
    assets" declines to move.
    """
    cmds, log = _run_retarget_body(remote_sha=_STALE_SHA, http_code='200',
                                   body_text='<html>502 Bad Gateway</html>')
    assert not _pushes(cmds), (
        f'an unreadable asset list is not proof of incompleteness — the tag '
        f'must be left alone; pushes={_pushes(cmds)!r}\nlog:\n{log}'
    )


# ── both gates must actually call the shared script ───────────────

@pytest.mark.parametrize('job,step_finder', [
    ('version', lambda wf: _version_gate_script()),
    ('release', lambda wf: next(
        s['run'] for s in wf['jobs']['release']['steps']
        if 'Assert all platform assets' in str(s.get('name', '')))),
])
def test_both_gates_delegate_to_the_shared_predicate(job, step_finder):
    """Neither gate may re-derive completeness on its own."""
    code = '\n'.join(ln for ln in step_finder(_workflow()).splitlines()
                     if not ln.lstrip().startswith('#'))
    assert 'scripts/release_assets.py' in code, (
        f'the {job} gate must call scripts/release_assets.py rather than '
        f'carrying its own copy of the expected-asset list'
    )


def test_the_shared_script_survives_export_and_is_tracked():
    """charter #13/#14: a file the pipeline needs must reach the repo that runs
    it — through BOTH doors, export and git.

    The workflow ships in the opensource export, so if the script it calls were
    stripped (or left gitignored), the public tree would carry a release
    pipeline that dies on a missing file the first time it runs.

    Note the opensource mechanism, because it decides what to assert: all of
    ``scripts/`` is excluded WHOLESALE and then specific files are copied back
    by ``_restore_opensource_kept_files``. So for that mode the question is
    membership in the keep-list — ``_should_exclude`` returning a verdict there
    is expected and is NOT evidence of a problem.
    """
    _export = pytest.importorskip('export', reason='export.py not shipped in opensource')
    rel = 'scripts/release_assets.py'
    for mode in ('personal', 'internal'):
        verdict = _export._should_exclude('scripts', 'release_assets.py', mode)
        assert not verdict, (
            f'scripts/release_assets.py is stripped from the {mode} export '
            f'({verdict!r}), but build-desktop.yml calls it'
        )
    assert rel in _export._OPENSOURCE_KEEP_FILES, (
        f'{rel} is not in _OPENSOURCE_KEEP_FILES. The opensource export drops '
        f'all of scripts/ and restores only that list, so without an entry the '
        f'public tree ships a build-desktop.yml that calls a file it does not '
        f'contain — and the failure surfaces in CI, not locally.'
    )
    tracked = subprocess.run(
        ['git', 'ls-files', '--error-unmatch', rel],
        cwd=_ROOT, capture_output=True, text=True)
    assert tracked.returncode == 0, (
        'scripts/release_assets.py is not git-tracked — /scripts/* is '
        'gitignored, so it needs an explicit ! exception or it will be absent '
        'from a clean clone while both gates try to call it'
    )


# ══════════════════════════════════════════════════════════════════
#  Inno Setup wizard branding
# ══════════════════════════════════════════════════════════════════
#
# The installer's welcome/finish pages are the FIRST screens a Windows user
# ever sees of the product, and they shipped with Inno's DEFAULT blank art —
# the only unbranded surface left in the install flow (the web UI and the
# tk dialogs are fully branded). The fix lives in three places that must
# agree with each other, which is exactly what these pins enforce:
#
#   scripts/gen_desktop_icons.py  — PRODUCES wizard-large.bmp (164×314) and
#                                   wizard-small.bmp (55×58), the two sizes
#                                   Inno's `modern` style hard-requires.
#   .github/workflows/build-desktop.yml — the .iss heredoc REFERENCES them
#                                   (WizardImageFile / WizardSmallImageFile)
#                                   and the preflight REQUIRES them (the
#                                   same fail-early contract as the icon).
#
# A drift between any pair (generator stops emitting, .iss points elsewhere,
# preflight stops checking) turns the branded installer back into a default
# one — or worse, aborts the compile in ISCC with a cryptic error — with no
# test noticing. The parity test below runs the REAL generator and compares
# its output set against the paths the .iss references, so neither side can
# move without this suite going red.

_WIZARD_REFS = (
    ('WizardImageFile', 'wizard-large.bmp', (164, 314)),
    ('WizardSmallImageFile', 'wizard-small.bmp', (55, 58)),
)


def _icons_mod():
    """Import scripts/gen_desktop_icons.py (not a package, so load by path)."""
    spec = _ilu.spec_from_file_location(
        '_tofu_gen_desktop_icons', _ROOT / 'scripts' / 'gen_desktop_icons.py')
    assert spec and spec.loader, 'cannot load scripts/gen_desktop_icons.py'
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_inno_script_references_brand_wizard_images():
    """Ratchet: the .iss heredoc must carry BOTH Wizard*File directives
    pointing into static\\icons\\installer\\ (neuter either line → red)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    for directive, bmp, _size in _WIZARD_REFS:
        ref = f'{directive}=static\\\\icons\\\\installer\\\\{bmp}'
        assert ref in text, (
            f'{ref} missing from the installer.iss heredoc — the wizard '
            f'falls back to Inno default blank art'
        )


def test_preflight_requires_wizard_bitmaps():
    """Ratchet: the Verify-build-inputs step must require both BMPs with a
    fail-early message (neuter the loop → red), mirroring the SetupIconFile
    contract that turns a cryptic ISCC abort into a readable CI error.

    Anchored to text that exists ONLY in the preflight — a bare `bmp in
    text` is also satisfied by the .iss WizardImageFile line, which let a
    neutered preflight pass (measured NEUTER-miss 2026-08-01)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert 'wizard-large.bmp wizard-small.bmp' in text, (
        'the preflight for-loop no longer names both bitmaps'
    )
    assert 'icons/installer/$bmp' in text, (
        'the preflight no longer tests static/icons/installer/$bmp — a '
        'missing bitmap surfaces as an ISCC abort instead of a clear message'
    )


def test_generator_emits_exactly_the_referenced_bitmaps(tmp_path):
    """Parity, the strong pin: run the REAL generator into a temp icons dir
    and require that (a) every bitmap the .iss references is produced, and
    (b) each carries Inno's exact contract dimensions and BMP format. This
    bites in BOTH drift directions — a renamed output and a resized canvas
    are equally red."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    mod.ICONS_DIR = str(tmp_path)  # redirect outputs; SOURCE_PNG stays real
    mod.main()
    for directive, bmp, size in _WIZARD_REFS:
        out = tmp_path / 'installer' / bmp
        assert out.is_file(), (
            f'generator did not produce installer/{bmp}, but the .iss '
            f'references it as {directive} — ISCC would abort on the missing '
            f'file'
        )
        with Image.open(str(out)) as im:
            assert im.format == 'BMP', f'{bmp}: ISCC needs BMP, got {im.format}'
            assert im.size == size, (
                f'{bmp}: Inno modern style requires {size}, got {im.size}'
            )


def test_wizard_logo_has_real_transparency_cutout():
    """The brand cube must FLOAT on the wizard gradient, not sit in a white
    square: logo.png is RGBA but fully opaque (alpha 255 everywhere), so the
    generator must cut out the exterior background. NEUTER target: removing
    _cut_out_background turns the large bitmap's border pixels white — this
    test reads the actual generated BMP and goes red."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    src = Image.open(mod.SOURCE_PNG).convert('RGBA')
    cut = mod._cut_out_background(src)
    alpha = cut.getchannel('A')
    # Corners are exterior background → must be transparent after the cutout.
    for xy in ((0, 0), (src.width - 1, 0), (0, src.height - 1),
               (src.width - 1, src.height - 1)):
        assert alpha.getpixel(xy) == 0, (
            f'corner {xy} is still opaque — the flood-fill cutout is broken '
            f'(or removed), and the wizard logo renders as a white square'
        )
    # The cube's centre must stay opaque (the cutout removes ONLY the
    # exterior; an over-eager global threshold would punch the interior).
    assert alpha.getpixel((src.width // 2, src.height // 2)) == 255, (
        'cube centre went transparent — the cutout leaks into the interior'
    )


# ══════════════════════════════════════════════════════════════════
#  App icon (.ico / .icns) — no baked-in white plate
# ══════════════════════════════════════════════════════════════════
#
# The wizard BMPs and the DMG art cut the white canvas out so the cube
# FLOATS on their gradients — but main() fed the RAW opaque source to
# gen_ico / gen_icns, so every icon frame shipped the white canvas as an
# opaque background. Result (owner screenshot 2026-08-03): the desktop
# icon rendered as a white plate with a tofu on it — measured 100% opaque,
# corners (254,255,255,255), on the shipped static/icons/tofu.ico. The fix:
# ico/icns are generated from the same flood-fill cutout, cropped to the
# cube's bbox with a small margin so the icon fills the frame instead of
# shrinking into its whitespace. These pins read the REAL generator output
# into a temp dir, so reverting to the opaque source (or dropping the
# crop) goes red.


def test_app_icon_frames_have_transparent_corners(tmp_path):
    """The .ico frames must have REAL transparency around the cube —
    neuter target: gen_ico(src) on the raw opaque canvas makes every
    corner pixel opaque white (the exact defect the owner reported)."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    mod.ICONS_DIR = str(tmp_path)  # redirect outputs; SOURCE_PNG stays real
    mod.main()
    ico = Image.open(str(tmp_path / 'tofu.ico'))
    ico.size = max(ico.ico.sizes())
    ico.load()
    alpha = ico.convert('RGBA').getchannel('A')
    w, h = alpha.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert alpha.getpixel(xy) == 0, (
            f'corner {xy} is opaque — the white canvas is baked into the '
            f'icon again (gen_ico must receive the cutout, not raw source)'
        )
    # The cube's centre must stay opaque (the cutout removes ONLY the
    # exterior; an over-eager global threshold would punch the interior).
    assert alpha.getpixel((w // 2, h // 2)) == 255, (
        'cube centre went transparent — the cutout leaks into the interior'
    )


def test_app_icon_cube_fills_the_frame(tmp_path):
    """With the plate removed the cube must still FILL the frame: the
    generator crops to the artwork bbox with a small margin. A bare
    cutout-without-crop leaves ~35% empty margin — the tofu renders tiny
    on the desktop and illegible in the 16px tray."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    mod.ICONS_DIR = str(tmp_path)
    mod.main()
    ico = Image.open(str(tmp_path / 'tofu.ico'))
    ico.size = max(ico.ico.sizes())
    ico.load()
    alpha = ico.convert('RGBA').getchannel('A')
    w, h = alpha.size
    hist = alpha.histogram()
    opaque = sum(hist[129:]) / (w * h)
    assert opaque > 0.45, (
        f'cube covers only {opaque:.0%} of the frame — the bbox crop is '
        f'gone and the icon shrank back into its margins'
    )


def test_app_icns_also_has_transparent_corners(tmp_path):
    """The macOS .icns shares the same defect path (same raw source in
    main()) — pin its corners too so a future split of the two generators
    cannot quietly reintroduce the plate on one platform."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    mod.ICONS_DIR = str(tmp_path)
    mod.main()
    icns = Image.open(str(tmp_path / 'tofu.icns'))
    rgba = icns.convert('RGBA')
    alpha = rgba.getchannel('A')
    w, h = alpha.size
    for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
        assert alpha.getpixel(xy) == 0, (
            f'icns corner {xy} is opaque — white canvas baked into the '
            f'macOS icon again'
        )


# ══════════════════════════════════════════════════════════════════
#  DMG window branding
# ══════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════
#  DMG window branding
# ══════════════════════════════════════════════════════════════════
#
# The macOS installer shipped as a DEFAULT WHITE FINDER WINDOW: no
# --background at all, so nothing told the user the one thing a DMG window
# exists to say — drag the app onto Applications. The artwork (warm paper
# gradient + brand cube + a drag arrow) is drawn BETWEEN the two icon
# positions, which makes the arrow's coordinates and create-dmg's --icon /
# --app-drop-link coordinates ONE fact stored in two places: move an icon
# on only one side and the arrow points at empty space. The alignment test
# below compares the workflow's parsed coordinates against the generator's
# constants, so neither side can drift alone.

_DMG_BG_REF = '--background "static/icons/installer/dmg-background.png"'


def test_dmg_step_uses_brand_background():
    """Ratchet: create-dmg must carry --background with the generated art
    (neuter the flag → red; the default white Finder window comes back)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert _DMG_BG_REF in text, (
        'create-dmg lost its --background — the DMG renders as an unbranded '
        'white Finder window again'
    )


def test_dmg_step_preflights_the_background():
    """Ratchet: a missing artwork file is a BUILD error (icon step broke),
    and must fail loudly BEFORE create-dmg — not fall through to the zip
    fallback with an unbranded window (neuter the check → red)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert 'icons/installer/dmg-background.png not generated' in text, (
        'no fail-early check on the DMG artwork — a missing file degrades '
        'silently into the zip fallback'
    )


def test_generator_emits_dmg_backgrounds(tmp_path):
    """Parity: the generator must produce BOTH the 1x artwork the workflow
    references and the @2x retina sibling create-dmg picks up automatically,
    at exactly window size ×1/×2."""
    Image = pytest.importorskip('PIL.Image', reason='Pillow required')
    mod = _icons_mod()
    mod.ICONS_DIR = str(tmp_path)
    mod.main()
    for name, size in (('dmg-background.png', mod.DMG_BG_SIZE),
                       ('dmg-background@2x.png',
                        (mod.DMG_BG_SIZE[0] * 2, mod.DMG_BG_SIZE[1] * 2))):
        out = tmp_path / 'installer' / name
        assert out.is_file(), f'generator did not produce installer/{name}'
        with Image.open(str(out)) as im:
            assert im.format == 'PNG', f'{name}: create-dmg needs PNG'
            assert im.size == size, f'{name}: expected {size}, got {im.size}'


def test_dmg_artwork_arrow_matches_icon_coordinates():
    """Alignment pin: the workflow's --icon / --app-drop-link coordinates
    must equal the generator's DMG_APP_ICON_POS / DMG_DROP_ICON_POS — the
    arrow in the artwork is drawn between those two points, so a one-sided
    edit makes the art point at empty space (and only this test notices)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    app = re.search(r'--icon "Tofu\.app" (\d+) (\d+)', text)
    drop = re.search(r'--app-drop-link (\d+) (\d+)', text)
    assert app and drop, 'create-dmg icon coordinates not found in workflow'
    mod = _icons_mod()
    assert (int(app.group(1)), int(app.group(2))) == mod.DMG_APP_ICON_POS, (
        f'--icon says {app.groups()} but the artwork is drawn for '
        f'{mod.DMG_APP_ICON_POS}'
    )
    assert (int(drop.group(1)), int(drop.group(2))) == mod.DMG_DROP_ICON_POS, (
        f'--app-drop-link says {drop.groups()} but the artwork is drawn for '
        f'{mod.DMG_DROP_ICON_POS}'
    )


# ══════════════════════════════════════════════════════════════════
#  Linux desktop integration
# ══════════════════════════════════════════════════════════════════
#
# The Linux artifact was a BARE tar.gz: extract, find the binary, run it
# from a terminal. No application-menu entry, no icon — the only platform
# with zero install UX. The tarball now ships install.sh (per-user, no
# sudo, idempotent) + a .desktop template, and the workflow copies both
# into the bundle BEFORE archiving. The pins below cover the three places
# that must agree: the source files, the copy step, and the template the
# script renders.

_DESKTOP_ENTRY = _ROOT / 'desktop' / 'tofu.desktop'
_INSTALL_LINUX = _ROOT / 'desktop' / 'install-linux.sh'


def test_linux_leg_ships_integration_before_archiving():
    """Ratchet: the workflow must copy install.sh + tofu.desktop into the
    bundle, and BEFORE `tar` runs — a tarball without them is a silent UX
    regression (neuter the step → red)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert 'desktop/install-linux.sh dist/Tofu/install.sh' in text, (
        'install.sh is not copied into the Linux bundle'
    )
    assert 'desktop/tofu.desktop dist/Tofu/tofu.desktop' in text, (
        'tofu.desktop is not copied into the Linux bundle'
    )
    copy_at = text.index('desktop/install-linux.sh')
    tar_at = text.index('tar czf')
    assert copy_at < tar_at, (
        'the integration files are copied AFTER the archive step — the '
        'tarball would not contain them'
    )


def test_desktop_entry_template_valid():
    """The .desktop template must carry the freedesktop-required keys and
    the __INSTALL_DIR__ placeholder install.sh substitutes."""
    text = _DESKTOP_ENTRY.read_text(encoding='utf-8')
    assert text.startswith('[Desktop Entry]'), 'missing the section header'
    for needle in ('Type=Application', 'Name=Tofu',
                   'Exec=__INSTALL_DIR__/Tofu', 'Icon=tofu',
                   'Terminal=false'):
        assert needle in text, f'tofu.desktop is missing {needle}'


def test_install_linux_script_contract():
    """install.sh: syntax-clean bash, per-user paths ONLY (a sudo anywhere
    breaks the no-root contract that matches the Windows per-user install),
    renders __INSTALL_DIR__, registers icon + menu entry."""
    text = _INSTALL_LINUX.read_text(encoding='utf-8')
    # Scan CODE only: the comments legitimately say "no sudo", and a naive
    # substring check bites them (measured 2026-08-01).
    code = '\n'.join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith('#'))
    assert 'sudo' not in code, (
        'install-linux.sh uses sudo — the Linux install is supposed to be '
        'per-user like the Windows one'
    )
    for needle in ('XDG_DATA_HOME', 'hicolor', '__INSTALL_DIR__',
                   'update-desktop-database'):
        assert needle in text, f'install-linux.sh is missing {needle}'
    bash = subprocess.run(['bash', '-n', str(_INSTALL_LINUX)],
                          capture_output=True, text=True)
    assert bash.returncode == 0, f'bash syntax check failed: {bash.stderr}'


def test_install_linux_script_end_to_end(tmp_path):
    """Behaviour, not just contract: lay out a SIMULATED bundle (binary +
    icon + template + install.sh), run the script with HOME redirected to a
    temp dir, and require the two real outputs — a rendered menu entry whose
    Exec points at the bundle's absolute path, and the themed icon. This is
    the test that would catch a broken sed substitution or a wrong relative
    path even though every content pin above still passes."""
    pytest.importorskip('PIL.Image', reason='Pillow required')
    # Spaces and sed/desktop-entry metacharacters are normal in an extracted
    # download path. The pre-hardening installer generated a broken Exec line
    # here: spaces were unquoted, '&' expanded the sed match, and '%' was
    # interpreted as a desktop-entry field-code prefix.
    bundle = tmp_path / 'Tofu bundle & 100% $cash'
    (bundle / '_internal' / 'static' / 'icons').mkdir(parents=True)
    home = tmp_path / 'home'
    home.mkdir()
    (bundle / 'Tofu').write_text('#!/bin/sh\n', encoding='utf-8')
    (bundle / 'Tofu').chmod(0o755)
    # A real (tiny) PNG stands in for the logo — the script only copies it.
    import shutil
    shutil.copy(_ROOT / 'static' / 'icons' / 'logo.png',
                bundle / '_internal' / 'static' / 'icons' / 'logo.png')
    shutil.copy(_DESKTOP_ENTRY, bundle / 'tofu.desktop')
    shutil.copy(_INSTALL_LINUX, bundle / 'install.sh')

    env = dict(os.environ, HOME=str(home))
    r = subprocess.run(['bash', str(bundle / 'install.sh')],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, f'install.sh failed: {r.stderr}'

    entry = home / '.local' / 'share' / 'applications' / 'tofu.desktop'
    assert entry.is_file(), 'no application-menu entry was written'
    body = entry.read_text(encoding='utf-8')
    desktop_exec_dir = str(bundle).replace('$', r'\$').replace('%', '%%')
    assert f'Exec="{desktop_exec_dir}/Tofu"' in body, (
        f'__INSTALL_DIR__ was not rendered to the bundle path: {body!r}'
    )
    assert '__INSTALL_DIR__' not in body, 'unrendered placeholder survived'
    icon = (home / '.local' / 'share' / 'icons' / 'hicolor' / '512x512'
            / 'apps' / 'tofu.png')
    assert icon.is_file(), 'no themed icon was installed'
    # Nothing may escape the fake HOME (the per-user contract, enforced).
    assert not (bundle / '.local').exists()


def test_install_linux_honours_xdg_data_home(tmp_path):
    """XDG_DATA_HOME is the desktop-standard authority when it is set."""
    pytest.importorskip('PIL.Image', reason='Pillow required')
    import shutil

    bundle = tmp_path / 'Tofu'
    (bundle / '_internal' / 'static' / 'icons').mkdir(parents=True)
    (bundle / 'Tofu').write_text('#!/bin/sh\n', encoding='utf-8')
    (bundle / 'Tofu').chmod(0o755)
    shutil.copy(_ROOT / 'static' / 'icons' / 'logo.png',
                bundle / '_internal' / 'static' / 'icons' / 'logo.png')
    shutil.copy(_DESKTOP_ENTRY, bundle / 'tofu.desktop')
    shutil.copy(_INSTALL_LINUX, bundle / 'install.sh')

    home = tmp_path / 'home'
    data_home = tmp_path / 'custom-data'
    home.mkdir()
    env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(data_home))
    result = subprocess.run(
        ['bash', str(bundle / 'install.sh')],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (data_home / 'applications' / 'tofu.desktop').is_file()
    assert (data_home / 'icons' / 'hicolor' / '512x512' / 'apps'
            / 'tofu.png').is_file()
    assert not (home / '.local' / 'share' / 'applications'
                / 'tofu.desktop').exists()


def test_install_linux_help_and_unknown_args_are_side_effect_free(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path / 'home'))

    help_result = subprocess.run(
        ['bash', str(_INSTALL_LINUX), '--help'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert help_result.returncode == 0
    assert 'usage: ./install.sh' in help_result.stdout
    assert '--uninstall' in help_result.stdout

    bad_result = subprocess.run(
        ['bash', str(_INSTALL_LINUX), '--typo'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert bad_result.returncode == 2
    assert 'unknown arguments: --typo' in bad_result.stderr
    assert not (tmp_path / 'home').exists()


def test_install_linux_uninstall_removes_only_desktop_integration(tmp_path):
    pytest.importorskip('PIL.Image', reason='Pillow required')
    import shutil

    bundle = tmp_path / 'Tofu'
    (bundle / '_internal' / 'static' / 'icons').mkdir(parents=True)
    (bundle / 'Tofu').write_text('#!/bin/sh\n', encoding='utf-8')
    (bundle / 'Tofu').chmod(0o755)
    shutil.copy(_ROOT / 'static' / 'icons' / 'logo.png',
                bundle / '_internal' / 'static' / 'icons' / 'logo.png')
    shutil.copy(_DESKTOP_ENTRY, bundle / 'tofu.desktop')
    shutil.copy(_INSTALL_LINUX, bundle / 'install.sh')
    home = tmp_path / 'home'
    home.mkdir()
    env = dict(os.environ, HOME=str(home))

    installed = subprocess.run(
        ['bash', str(bundle / 'install.sh')],
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    entry = home / '.local' / 'share' / 'applications' / 'tofu.desktop'
    icon = (home / '.local' / 'share' / 'icons' / 'hicolor' / '512x512'
            / 'apps' / 'tofu.png')
    assert entry.is_file() and icon.is_file()

    removed = subprocess.run(
        ['bash', str(bundle / 'install.sh'), '--uninstall'],
        capture_output=True,
        text=True,
        env=env,
    )
    assert removed.returncode == 0, removed.stderr
    assert not entry.exists()
    assert not icon.exists()
    assert (bundle / 'Tofu').is_file()
    assert 'bundle' in removed.stdout and 'kept' in removed.stdout


# ══════════════════════════════════════════════════════════════════
#  Linux .deb installer (pt_a64216b959694605)
# ══════════════════════════════════════════════════════════════════
#
# Evaluated .deb vs AppImage for the "double-click install" Linux format;
# .deb won on every axis that survives contact with a real machine:
# dpkg-deb ships in the CI base image (zero downloaded tooling — appimagetool
# would be a third-party binary fetched every build), no FUSE at build time
# (mksquashfs is absent on the dev host, measured 2026-08-01), and — the
# decisive one — no FUSE at RUN time: type-2 AppImages need libfuse2, which
# Ubuntu 22.04+ no longer installs by default, turning "double-click" into
# "sudo apt install libfuse2 first". The tarball stays as the no-sudo /
# non-Debian fallback; the two formats are complements.
#
# The .deb is a REQUIRED release asset (scripts/release_assets.py owns the
# list), so it flows into BOTH completeness gates and the in-app download
# surface from one row — these pins cover the build step, the pipeline
# plumbing, the shared-list row, and the script's real output.

_BUILD_DEB = _ROOT / 'desktop' / 'build-deb.sh'


def test_linux_leg_builds_and_uploads_deb():
    """Ratchet: the Linux leg must run build-deb.sh and upload the .deb
    alongside the tarball (neuter either → red)."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert 'desktop/build-deb.sh dist/Tofu' in text, (
        'no build-deb step — the Linux leg ships no .deb'
    )
    assert 'Tofu-*.deb' in text, 'the .deb is not uploaded as an artifact'


def test_release_pipeline_ships_deb():
    """Ratchet: the .deb must be checksummed AND attached to the release —
    an asset that is built but not shipped is invisible to users."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert '*.deb' in text.split('SHA256SUMS')[0] or 'files=( *.exe *.dmg *.zip *.tar.gz *.deb )' in text, (
        'SHA256SUMS does not cover *.deb'
    )
    assert 'release-assets/**/*.deb' in text, (
        'the release action does not attach *.deb'
    )


def test_deb_is_a_required_release_asset():
    """Behavioural: the shared completeness list must (a) contain the deb
    row, and (b) judge an old-style 4-asset release INCOMPLETE — that is
    what makes the next build repair-ship the deb through BOTH gates."""
    mod = _asset_mod()
    deb_rows = [a for a in mod.PLATFORM_ASSETS
                if a[0] == 'linux' and a[3].endswith('.deb')]
    assert deb_rows, 'no linux .deb row in PLATFORM_ASSETS'
    old_style = ['Tofu-0.15.2-macos-arm64.dmg',
                 'Tofu-0.15.2-macos-x86_64.dmg',
                 'Tofu-Setup-0.15.2-win64.exe',
                 'Tofu-0.15.2-linux-x86_64.tar.gz',
                 'SHA256SUMS']
    gaps = mod.missing_assets(old_style, require_checksums=True)
    assert any('.deb' in g for g in gaps), (
        f'an asset set without the deb was judged COMPLETE: {gaps}'
    )
    undersized = mod.undersized_assets(
        {'Tofu-0.15.2-linux-x86_64.deb': 49_000_000})
    assert undersized, 'a hollow 49 MB deb was not flagged'


def test_build_deb_script_contract():
    """build-deb.sh: syntax-clean, dpkg-deb-driven, /opt layout, postinst
    hook, no sudo (code lines only — comments legitimately discuss it)."""
    text = _BUILD_DEB.read_text(encoding='utf-8')
    code = '\n'.join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith('#'))
    assert 'sudo' not in code, 'build-deb.sh must not need root'
    for needle in ('dpkg-deb --build', '/opt/Tofu', 'postinst',
                   'usr/share/applications', '__INSTALL_DIR__'):
        assert needle in text, f'build-deb.sh is missing {needle}'
    assert text.index('unset LD_LIBRARY_PATH') < text.index('cp -a "$BUNDLE/."'), (
        'host packaging tools must not inherit Conda runtime libraries'
    )
    bash = subprocess.run(['bash', '-n', str(_BUILD_DEB)],
                          capture_output=True, text=True)
    assert bash.returncode == 0, f'bash syntax check failed: {bash.stderr}'


def test_build_deb_help_and_bad_arguments_are_side_effect_free(tmp_path):
    help_result = subprocess.run(
        ['bash', str(_BUILD_DEB), '--help'], cwd=tmp_path,
        capture_output=True, text=True)
    bad_result = subprocess.run(
        ['bash', str(_BUILD_DEB), 'bundle-only'], cwd=tmp_path,
        capture_output=True, text=True)
    version_result = subprocess.run(
        ['bash', str(_BUILD_DEB), str(tmp_path), '../bad'], cwd=tmp_path,
        capture_output=True, text=True)

    assert help_result.returncode == 0
    assert 'BUNDLE_DIR VERSION [OUT_DIR]' in help_result.stdout
    assert help_result.stderr == ''
    assert bad_result.returncode == 2
    assert 'expected BUNDLE_DIR VERSION [OUT_DIR]' in bad_result.stderr
    assert version_result.returncode == 2
    assert 'invalid Debian package version' in version_result.stderr
    assert list(tmp_path.iterdir()) == []


def test_build_deb_end_to_end(tmp_path):
    """Run the REAL script on a simulated bundle and inspect the package
    with dpkg-deb itself: metadata, payload paths, and the rendered menu
    entry. Skipped where dpkg-deb is unavailable (non-Debian hosts) — the
    CI runners always have it."""
    import shutil
    if shutil.which('dpkg-deb') is None:
        pytest.skip('dpkg-deb not available on this host')
    bundle = tmp_path / 'Tofu'
    (bundle / '_internal' / 'static' / 'icons').mkdir(parents=True)
    (bundle / 'Tofu').write_text('#!/bin/sh\n', encoding='utf-8')
    (bundle / 'Tofu').chmod(0o755)
    shutil.copy(_ROOT / 'static' / 'icons' / 'logo.png',
                bundle / '_internal' / 'static' / 'icons' / 'logo.png')

    r = subprocess.run(
        ['bash', str(_BUILD_DEB), str(bundle), '9.9.9', str(tmp_path)],
        capture_output=True, text=True)
    assert r.returncode == 0, f'build-deb.sh failed: {r.stderr}'

    deb = tmp_path / 'Tofu-9.9.9-linux-x86_64.deb'
    assert deb.is_file(), f'no .deb produced: {r.stdout} {r.stderr}'

    info = subprocess.run(['dpkg-deb', '--info', str(deb)],
                          capture_output=True, text=True)
    assert info.returncode == 0, info.stderr
    for field in ('Package: tofu', 'Version: 9.9.9', 'Architecture: amd64'):
        assert field in info.stdout, f'{field} missing from control:\n{info.stdout}'

    contents = subprocess.run(['dpkg-deb', '--contents', str(deb)],
                              capture_output=True, text=True)
    assert contents.returncode == 0, contents.stderr
    for path in ('./opt/Tofu/Tofu',
                 './usr/share/applications/tofu.desktop',
                 './usr/share/icons/hicolor/512x512/apps/tofu.png'):
        assert path in contents.stdout, f'{path} missing from payload'

    fsys = subprocess.run(
        f"dpkg-deb --fsys-tarfile '{deb}' | tar -xO ./usr/share/applications/tofu.desktop",
        shell=True, capture_output=True, text=True)
    assert fsys.returncode == 0, fsys.stderr
    assert 'Exec=/opt/Tofu/Tofu' in fsys.stdout, (
        f'__INSTALL_DIR__ not rendered to /opt/Tofu:\n{fsys.stdout}'
    )
