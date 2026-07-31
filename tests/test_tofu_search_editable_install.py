"""`tofu_search` must be imported from its source tree, not a stale copy.

★ Why this exists (measured 2026-07-28, not hypothetical):

`tofu_search` was installed as a plain COPY under site-packages while its source
tree lived beside chatui. Both reported version `0.5.2`, but the copy was 30+
`.py` files behind — so the version number, the obvious thing to check, actively
argued that the copy was current.

Today's symptom was loud: new functions written in the source tree were simply
absent at runtime (`AttributeError`). The dangerous direction is the silent one
— fix a bug in the source tree, leave the copy stale, and every guard in the
source repo goes green while production keeps running the old code. A copy
install turns "the tests pass" into a statement about a file nobody executes.

Both assertions are required. Asserting only the load path leaves the door open
for a later `pip install tofu-search` (no `-e`) to drop a physical package
alongside the editable finder, at which point `sys.path` order silently decides
which one wins — and it may still satisfy a load-path check while production
runs the other.

If this fails, re-run:  pip install -e ../tofu-search
"""

import pathlib
import sysconfig

import pytest

import tofu_search


def test_tofu_search_loads_from_a_source_checkout():
    """The imported package must resolve to a working tree, not site-packages."""
    loaded = pathlib.Path(tofu_search.__file__).resolve()
    site = pathlib.Path(sysconfig.get_paths()['purelib']).resolve()

    assert not loaded.is_relative_to(site), (
        f'tofu_search is being imported from site-packages ({loaded}).\n'
        'That is a COPY install: edits to the source tree will not take effect, '
        'and — worse — a fix made in the source repo can look verified there '
        'while production keeps running this stale copy.\n'
        'Fix with:  pip install -e ../tofu-search'
    )

    # A source checkout has the packaging metadata a copy install lacks.
    project_root = loaded.parent.parent
    assert (project_root / 'pyproject.toml').is_file(), (
        f'{loaded} does not look like a source checkout — no pyproject.toml at '
        f'{project_root}. Expected an editable install pointing at the '
        'tofu-search repository.'
    )


def test_no_physical_copy_shadows_the_editable_install():
    """site-packages must not also contain a real `tofu_search/` directory.

    The complement of the test above. With both an editable finder and a
    physical directory present, which one wins depends on `sys.path` ordering —
    so the load-path assertion alone can pass while production imports the copy.
    """
    site = pathlib.Path(sysconfig.get_paths()['purelib'])
    shadow = site / 'tofu_search'

    if shadow.is_dir() and not shadow.is_symlink():
        pytest.fail(
            f'A physical tofu_search package still exists at {shadow}, '
            'shadowing (or racing) the editable install. Remove it with:\n'
            '  pip uninstall -y tofu-search && pip install -e ../tofu-search'
        )
