"""The request path consumes prebuilt Vite assets and never builds frontend code."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

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
    assert 'validate_published_vite_artifact' in source
    assert 'subprocess' not in source


@pytest.mark.parametrize('role', ('all', 'api'))
def test_frontend_roles_fail_startup_with_actionable_artifact_error(
        role, monkeypatch):
    import lib.server_assembly as assembly
    from lib import vite_assets

    monkeypatch.setattr(
        assembly, '_DEPLOYMENT_CONFIGURATION',
        SimpleNamespace(process_role=role))
    monkeypatch.setattr(assembly, '_server_log', logging.getLogger(__name__))
    monkeypatch.setattr(
        vite_assets, 'validate_published_vite_artifact',
        lambda: (_ for _ in ()).throw(RuntimeError('manifest missing')))

    with pytest.raises(RuntimeError) as error:
        assembly._check_frontend_artifact()

    message = str(error.value)
    assert f'process role {role}' in message
    assert 'manifest missing' in message
    assert 'npm run build:frontend' in message


@pytest.mark.parametrize('role', ('worker', 'scheduler'))
def test_non_frontend_roles_skip_artifact_validation(role, monkeypatch):
    import lib.server_assembly as assembly
    from lib import vite_assets

    boot = []
    monkeypatch.setattr(
        assembly, '_DEPLOYMENT_CONFIGURATION',
        SimpleNamespace(process_role=role))
    monkeypatch.setattr(assembly, '_boot', lambda *args: boot.append(args))
    monkeypatch.setattr(
        vite_assets, 'validate_published_vite_artifact',
        lambda: pytest.fail('non-frontend role validated browser assets'))

    assembly._check_frontend_artifact()

    assert boot and role in boot[0]


def test_runtime_startup_does_not_recheck_authoring_source_freshness(
        monkeypatch):
    import lib.server_assembly as assembly
    from lib import vite_assets

    calls = []
    monkeypatch.setattr(
        assembly, '_DEPLOYMENT_CONFIGURATION',
        SimpleNamespace(process_role='all'))
    monkeypatch.setattr(
        vite_assets, 'validate_vite_artifact',
        lambda: pytest.fail(
            'ASGI recovery must not treat authoring drift as graph corruption'))
    monkeypatch.setattr(
        vite_assets, 'validate_published_vite_artifact',
        lambda: calls.append('published'))

    assembly._check_frontend_artifact()

    assert calls == ['published']
