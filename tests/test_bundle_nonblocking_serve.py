"""The request path consumes prebuilt Vite assets and never builds frontend code."""

from __future__ import annotations

import inspect

import pytest


pytestmark = pytest.mark.unit


def test_index_page_uses_vite_asset_owner():
    import routes.common as common
    from lib import vite_assets

    assert common._get_vite_asset_tags is vite_assets.get_vite_asset_tags
    source = inspect.getsource(common)
    assert 'js_bundler' not in source
    assert 'subprocess' not in source


def test_server_does_not_build_missing_frontend_artifacts():
    import server

    source = inspect.getsource(server._check_frontend_artifact)
    assert 'validate_vite_artifact' in source
    assert 'build' not in source.lower()
    assert 'subprocess' not in source
