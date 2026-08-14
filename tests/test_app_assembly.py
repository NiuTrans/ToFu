"""Independent native Quart application assembly contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.app_assembly import create_application
from lib.http_body_policy import HttpBodyPolicy


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


def test_server_factory_does_not_mutate_or_return_assembled_singleton(flask_app):
    import server

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


def test_server_production_factory_attaches_lifespan_without_starting_it():
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
