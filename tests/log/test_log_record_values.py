"""Diagnostic bounds preserve original credential values."""
import logging

import pytest

from lib.logging_pkg.log_record_values import BoundedFormatter, sanitize_value, text_value

pytestmark = pytest.mark.unit


def test_text_preserves_original_without_scanning_or_copying():
    text = 'api_key=example password=sample Cookie: value ' + 'A' * (4 * 1024 * 1024)
    assert text_value(text) is text


def test_structured_credentials_and_urls_are_preserved():
    value = {'api_key': 'example', 'authorization': 'Bearer sample',
             'nested': {'password': 'sample', 'cookie': 'example'},
             'url': 'https://user:password@example.test/?token=sample'}
    assert sanitize_value(value) == value


def test_formatter_preserves_credentials_and_bounds_output():
    record = logging.LogRecord('test', logging.INFO, __file__, 1,
                               'password=sample ' + 'x' * 20000, (), None)
    text = BoundedFormatter('%(message)s', max_chars=4096).format(record)
    assert 'password=sample' in text
    assert '<redacted>' not in text
    assert len(text) <= 4096


def test_route_debug_logging_preserves_credentials(monkeypatch, caplog):
    from types import SimpleNamespace
    import quart
    from lib.log import log_route
    logger = logging.getLogger('test.route.values')
    monkeypatch.setattr(quart, 'request', SimpleNamespace(
        method='POST', path='/sample',
        get_json=lambda **_: {'password': 'sample-value', 'api_key': 'example-key'}))
    @log_route(logger, log_request_body=True)
    def handler():
        return {'ok': True}
    with caplog.at_level(logging.DEBUG, logger='test.route.values'):
        assert handler() == {'ok': True}
    assert 'sample-value' in caplog.text
    assert 'example-key' in caplog.text
