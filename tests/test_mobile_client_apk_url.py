"""Guard the two-place coupling between the mobile-client download URL and the
Android CI workflow's published asset name.

The Tofu backend's default download URL is a DIRECT deep link
(``…/releases/latest/download/<asset>``). For a tap on that link to resolve
instead of 404ing, the Android CI workflow
(``tofu-android/.github/workflows/build-apk.yml``) must publish a release asset
whose filename is the SAME string as the one embedded in that URL. Those two
live in different repos, so nothing but this test keeps them in lockstep — the
exact kind of coupling that rots into a permanent 404.

Assertions:
  1. Backend self-consistency (ALWAYS): the filename at the tail of
     ``DEFAULT_MOBILE_CLIENT_URL`` equals ``MOBILE_CLIENT_APK_ASSET``, and the
     URL is a ``/releases/latest/download/`` deep link (not the bare page).
  2. Cross-repo coupling (when the sibling workflow is locatable): the workflow
     renames the release APK to that same asset name AND publishes exactly it.
"""
import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from routes.common import DEFAULT_MOBILE_CLIENT_URL, MOBILE_CLIENT_APK_ASSET


def test_backend_default_url_filename_matches_asset_constant():
    # The URL's trailing path segment must be the canonical asset name.
    assert DEFAULT_MOBILE_CLIENT_URL.rsplit('/', 1)[-1] == MOBILE_CLIENT_APK_ASSET
    # And it must be a direct-download deep link, not the wrong-platform page.
    assert '/releases/latest/download/' in DEFAULT_MOBILE_CLIENT_URL
    assert not DEFAULT_MOBILE_CLIENT_URL.rstrip('/').endswith('/releases/latest')
    # A real APK filename, so a phone treats the tap as a download.
    assert MOBILE_CLIENT_APK_ASSET.endswith('.apk')


def _locate_workflow() -> Path | None:
    """Find the Android CI workflow across the two-repo split, or None."""
    candidates = []
    env = os.environ.get('TOFU_ANDROID_DIR')
    if env:
        candidates.append(Path(env) / '.github/workflows/build-apk.yml')
    here = Path(__file__).resolve()
    # chatui/ and tofu-android/ are siblings under the same parent in dev.
    for parent in list(here.parents)[:6]:
        candidates.append(parent / 'tofu-android/.github/workflows/build-apk.yml')
        candidates.append(parent.parent / 'tofu-android/.github/workflows/build-apk.yml')
    for c in candidates:
        if c.is_file():
            return c
    return None


def test_ci_workflow_publishes_the_same_asset_name():
    wf = _locate_workflow()
    if wf is None:
        pytest.skip(
            'tofu-android workflow not locatable (separate repo). Set '
            'TOFU_ANDROID_DIR to run the cross-repo coupling assertion.'
        )
    text = wf.read_text(encoding='utf-8')
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
        '(that would publish Gradle\u2019s app-release.apk and break the deep link)'
    )
