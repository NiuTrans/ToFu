"""Independent native Quart application assembly contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.app.app_assembly import create_application
from lib.http_transport.body_policy import HttpBodyPolicy


pytestmark = pytest.mark.unit


def test_http_recipe_builds_isolated_instances_with_identical_routes(tmp_path):
    static_dir = tmp_path / 'static'
    static_dir.mkdir()
    (static_dir / 'probe.js').write_text('window.probe = true;')
    policy = HttpBodyPolicy(body_timeout=41, upload_body_timeout=91)
    logger = logging.getLogger('test.app-assembly')

    first = create_application(
        'assembly-first', static_dir=str(static_dir), logger=logger,
        secret_key='first-secret', config={'TESTING': True},
        body_policy=policy,
    )
    second = create_application(
        'assembly-second', static_dir=str(static_dir), logger=logger,
        secret_key='second-secret', config={'TESTING': True},
        body_policy=policy,
    )

    assert first is not second
    assert first.secret_key == 'first-secret'
    assert second.secret_key == 'second-secret'
    assert first.static_folder is None and second.static_folder is None
    assert first.config['BODY_TIMEOUT'] == second.config['BODY_TIMEOUT'] == 41
    assert first.extensions['tofu_http_application_assembled'] is True
    assert second.extensions['tofu_http_application_assembled'] is True
    assert set(first.blueprints) == set(second.blueprints)
    assert {rule.rule for rule in first.url_map.iter_rules()} == {
        rule.rule for rule in second.url_map.iter_rules()
    }

    async def exercise():
        response = await first.test_client().get('/static/probe.js')
        assert response.status_code == 200
        assert await response.get_data() == b'window.probe = true;'

    asyncio.run(exercise())


def test_server_factory_does_not_mutate_or_return_assembled_singleton():
    import server

    flask_app = server.app
    original_testing = flask_app.config['TESTING']
    first = server.create_app({'TESTING': True, 'FACTORY_SENTINEL': 'first'})
    second = server.create_app({'TESTING': True, 'FACTORY_SENTINEL': 'second'})

    assert first is not flask_app
    assert second is not flask_app
    assert first is not second
    assert first.config['FACTORY_SENTINEL'] == 'first'
    assert second.config['FACTORY_SENTINEL'] == 'second'
    assert flask_app.config.get('FACTORY_SENTINEL') is None
    assert flask_app.config['TESTING'] is original_testing
    assert {rule.rule for rule in first.url_map.iter_rules()} == {
        rule.rule for rule in flask_app.url_map.iter_rules()
    }
    assert first.extensions['tofu_lifecycle']['startup_handlers'] == (
        'tofu.logging.startup',
    )
    assert first.extensions['tofu_lifecycle']['shutdown_handlers'] == (
        'tofu.logging.shutdown',
    )


def test_production_factory_accepts_owned_lazy_native_image(monkeypatch):
    from pathlib import Path
    from unittest.mock import patch
    from lib.agent_core.native_image import NativeArchiveImage
    from tests.agent_core.test_native_process import budget
    import server
    image=NativeArchiveImage(archive=Path('/missing'),sha256='0'*64,
        application_version='fixture',budget=budget())
    with patch('lib.agent_core.native_artifact.seal_archive') as seal:
        app=server.create_production_app({'TESTING':True},native_image=image)
        assert app.extensions['tofu_native_host'].image is image
        assert 'tofu.native-host' in app.extensions['tofu_lifecycle']['shutdown_handlers']
        assert app.extensions['tofu_production_lifecycle']['status']=='registered'
        seal.assert_not_called()
    asyncio.run(app.extensions['tofu_native_host'].close())


def test_server_production_factory_attaches_lifespan_without_starting_it(
        monkeypatch):
    import server
    production_app = server.create_production_app({'TESTING': True})

    assert production_app is not server.app
    assert production_app.extensions[
        'tofu_production_lifecycle_registered'] is True
    assert production_app.extensions['tofu_production_lifecycle'][
        'status'] == 'registered'
    assert production_app.extensions['tofu_lifecycle']['startup_handlers'] == (
        'tofu.logging.startup',
        'tofu.serving-loop.startup',
        'tofu.production.startup',
    )
    assert production_app.extensions['tofu_lifecycle']['shutdown_handlers'] == (
        'tofu.logging.shutdown',
        'tofu.serving-loop.shutdown',
        'tofu.production.shutdown',
    )


def test_native_token_selection_requires_image_and_freezes_at_assembly(tmp_path):
    from lib.app.app_assembly import configure_application
    from tests.tasks_pkg.test_native_host import image
    options = dict(static_dir=str(tmp_path), logger=logging.getLogger('token-assembly'), secret_key='fixture')
    with pytest.raises(ValueError, match='explicit image'):
        create_application('missing-token-image', native_token_count=True, **options)
    with pytest.raises(ValueError, match='boolean'):
        create_application('invalid-token-selection', native_token_count=1, **options)
    release = image()
    app = create_application('token-assembly', native_image=release, native_token_count=True, **options)
    host = app.extensions['tofu_native_host']
    assert host.token_count_enabled and host._token_counter is None
    assert configure_application(app, native_image=release, native_token_count=True, **options) is False
    with pytest.raises(ValueError, match='differs'):
        configure_application(app, native_image=release, native_token_count=False, **options)
    asyncio.run(host.close())


@pytest.mark.parametrize('factory_name', ['create_app', 'create_production_app'])
@pytest.mark.parametrize('selected', [False, True])
def test_server_factories_preserve_lazy_owned_token_selection(factory_name, selected):
    from unittest.mock import patch
    from tests.tasks_pkg.test_native_host import image
    import server

    release = image()
    with patch('lib.agent_core.native_artifact.seal_archive') as seal:
        app = getattr(server, factory_name)(
            {'TESTING': True}, native_image=release, native_token_count=selected,
        )
        host = app.extensions['tofu_native_host']
        assert host.image is release
        assert host.token_count_enabled is selected
        assert host._token_counter is None
        assert 'tofu.native-host' in app.extensions['tofu_lifecycle']['shutdown_handlers']
        seal.assert_not_called()
        asyncio.run(host.close())


@pytest.mark.parametrize('factory_name', ['create_app', 'create_production_app'])
@pytest.mark.parametrize('invalid', [0, None, 'true'])
def test_server_factories_reject_non_boolean_token_selection(factory_name, invalid):
    import server

    with pytest.raises(ValueError, match='boolean'):
        getattr(server, factory_name)({'TESTING': True}, native_token_count=invalid)


@pytest.mark.parametrize('factory_name', ['create_app', 'create_production_app'])
def test_server_factories_require_explicit_image_for_token_selection(factory_name):
    import server

    with pytest.raises(ValueError, match='explicit image'):
        getattr(server, factory_name)({'TESTING': True}, native_token_count=True)


@pytest.mark.parametrize('factory_name', ['create_app', 'create_production_app'])
def test_server_factories_preserve_lazy_application_html_selection(factory_name):
    import server

    def unused():
        raise AssertionError('native HTML loaded before demand')

    app = getattr(server, factory_name)({'TESTING': True}, native_html_factory=unused)
    owner = app.extensions['tofu_html_projection']
    assert owner.factory is unused and owner.profile is None
    assert 'tofu.html-projection' in app.extensions['tofu_lifecycle']['shutdown_handlers']
    asyncio.run(owner.close())


def test_assembled_html_selection_cannot_be_replaced_or_added(tmp_path):
    from lib.app.app_assembly import configure_application
    options = dict(static_dir=str(tmp_path), logger=logging.getLogger('html-assembly'), secret_key='fixture')

    def unused():
        raise AssertionError('unused binding')

    app = create_application('selected', native_html_factory=unused, **options)
    assert configure_application(app, native_html_factory=unused, **options) is False
    with pytest.raises(ValueError, match='differs'):
        configure_application(app, native_html_factory=lambda: None, **options)
    plain = create_application('plain', **options)
    with pytest.raises(ValueError, match='differs'):
        configure_application(plain, native_html_factory=unused, **options)
    with pytest.raises(TypeError, match='callable'):
        create_application('invalid', native_html_factory=True, **options)
    asyncio.run(app.extensions['tofu_html_projection'].close())
