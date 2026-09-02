"""Guard the contract between the mobile-client download URL and the Android
CI workflow's published asset.

The Tofu backend's default download URL is a DIRECT deep link
(``…/releases/latest/download/<asset>``) on the **rangehow/tofu-android**
repo. For a tap on that link to resolve instead of 404ing, the in-tree
Android CI workflow (``.github/workflows/build-android-apk.yml``) must
publish a release asset whose filename is the SAME string as the one embedded
in that URL — and must publish it to that SAME repo.

Since the android source merged into this repo (2026-08-05, android/
subtree), the workflow lives in-tree, so every assertion here runs always —
there is no "workflow not locatable" skip anymore.

Assertions:
  1. Backend self-consistency: the filename at the tail of
     ``DEFAULT_MOBILE_CLIENT_URL`` equals ``MOBILE_CLIENT_APK_ASSET``, and the
     URL is a ``/releases/latest/download/`` deep link on the
     rangehow/tofu-android repo (not the bare page, not the ToFu repo — the
     ToFu release stream is desktop-versioned and would shadow the APK).
  2. Workflow pins the same asset name in the rename step AND in the
     action-gh-release ``files:`` list (no ``*.apk`` glob, which would publish
     Gradle's ``app-release.apk`` and break the deep link).
  3. The publish step targets ``repository: rangehow/tofu-android`` — without
     it the APK would land on THIS repo's release stream, where a newer
     desktop-only release would shadow ``/releases/latest`` into a 404.
  4. The workflow fires on the ``android-v*`` tag namespace ONLY (a bare
     ``v*`` trigger would race the desktop release tags v0.14.x) and is
     path-filtered to ``android/**`` (a backend-only push must not build APKs).
"""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from routes.common import DEFAULT_MOBILE_CLIENT_URL, MOBILE_CLIENT_APK_ASSET

_WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / '.github' / 'workflows' / 'build-android-apk.yml'
)
_RELEASE_REPO = 'rangehow/tofu-android'


def test_backend_default_url_filename_matches_asset_constant():
    # The URL's trailing path segment must be the canonical asset name.
    assert DEFAULT_MOBILE_CLIENT_URL.rsplit('/', 1)[-1] == MOBILE_CLIENT_APK_ASSET
    # And it must be a direct-download deep link, not the wrong-platform page.
    assert '/releases/latest/download/' in DEFAULT_MOBILE_CLIENT_URL
    assert not DEFAULT_MOBILE_CLIENT_URL.rstrip('/').endswith('/releases/latest')
    # A real APK filename, so a phone treats the tap as a download.
    assert MOBILE_CLIENT_APK_ASSET.endswith('.apk')
    # The deep link must point at the dedicated APK release repo. On the ToFu
    # repo, /releases/latest resolves to the NEWEST release — any desktop
    # release (v0.14.x, no APK asset) would shadow it into a permanent 404.
    assert f'github.com/{_RELEASE_REPO}/' in DEFAULT_MOBILE_CLIENT_URL


def test_ci_workflow_publishes_the_same_asset_name():
    assert _WORKFLOW.is_file(), f'Android CI workflow missing: {_WORKFLOW}'
    text = _WORKFLOW.read_text(encoding='utf-8')
    asset = MOBILE_CLIENT_APK_ASSET

    # The rename step must produce exactly the asset the URL points at.
    assert re.search(rf'release/{re.escape(asset)}\b', text), (
        f'workflow must rename/publish the release APK as {asset!r} so the '
        f'backend deep link resolves; got:\n{text}'
    )
    # Isolate the action-gh-release publish `files:` block (a `|` literal scalar
    # ending at the next key like `fail_on_unmatched_files:`). We assert against
    # the PUBLISH list specifically — a `*.apk` glob in the earlier rename `ls`
    # step is legitimate (Gradle's output name varies: app-release[-unsigned]).
    m = re.search(r'files:\s*\|\s*\n(.*?)\n\s*\w[\w-]*:', text, re.DOTALL)
    assert m, f'could not locate the publish files: block in the workflow:\n{text}'
    files_block = m.group(1)
    # The published asset must be the pinned canonical name…
    assert f'apk/release/{asset}' in files_block, (
        f'the action-gh-release files: list must publish {asset!r} exactly; '
        f'got files block:\n{files_block}'
    )
    # …and must NOT glob the release dir (which would upload Gradle's
    # app-release.apk and break the deep link's filename match).
    assert 'apk/release/*.apk' not in files_block, (
        'release publish must pin the asset name, not glob *.apk '
        '(that would publish Gradle’s app-release.apk and break the deep link)'
    )


def test_ci_workflow_publishes_to_the_apk_release_repo():
    """The deep link's repo and the publish target must be the same repo."""
    text = _WORKFLOW.read_text(encoding='utf-8')
    assert f'repository: {_RELEASE_REPO}' in text, (
        f'the publish step must target repository: {_RELEASE_REPO} — the '
        f'default download URL points there; publishing to THIS repo would '
        f'let a newer desktop-only release shadow /releases/latest into a 404'
    )
    # Cross-repo publish needs a PAT; the guard step must fail loudly without it.
    assert 'TOFU_ANDROID_RELEASE_PAT' in text, (
        'the workflow must reference secrets.TOFU_ANDROID_RELEASE_PAT for the '
        'cross-repo publish (and guard on its presence)'
    )


def test_ci_workflow_tag_namespace_and_path_filter():
    text = _WORKFLOW.read_text(encoding='utf-8')
    # The APK release fires on android-v* tags ONLY. A bare 'v*' would fire on
    # desktop release tags (v0.14.x, created by build-desktop.yml's release
    # step and export.py --bump pushes).
    assert "tags: ['android-v*']" in text, (
        "the tag trigger must be the android-v* namespace, not bare v* "
        "(desktop releases share this repo's tag space)"
    )
    assert 'branches: [main, master]' in text, (
        'Android changes on the default main branch must run the APK build'
    )
    assert re.search(r"tags:\s*\[?'v\*'?", text) is None, (
        'a bare v* tag trigger would fire an APK build on every desktop release'
    )
    # Cost control: backend-only pushes must not trigger Android builds.
    assert 'android/**' in text, (
        'the push/PR trigger must be path-filtered to android/** '
        '(a backend-only change must not spin up an Android SDK build)'
    )
