"""Large decoded text arguments retain exact failure and isolation contracts."""
import json

import pytest
from lib.llm.conversion import ProtocolConversionError, object_arguments

pytestmark = pytest.mark.unit
PADDING = 'text中文' * 20000


def test_large_text_object_returns_exact_detached_decoded_values():
    source = {'text': PADDING, 'values': [None, True, 7, -0.0, 1.25]}
    encoded = json.dumps(source)
    result = object_arguments(encoded)
    assert result == source
    result['values'].append('edited')
    assert object_arguments(encoded) == source


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity', '1e999', '-1e999'])
def test_large_encoded_nonfinite_values_keep_fixed_error(number):
    encoded = '{"text":' + json.dumps(PADDING) + ',"nested":[{"value":' + number + '}]}'
    with pytest.raises(ProtocolConversionError) as error:
        object_arguments(encoded)
    assert error.value.code == 'tool_arguments_invalid_json'


def test_wide_and_deep_decoded_shapes_keep_encoder_validation():
    nested = 1.25
    for _ in range(100): nested = [nested]
    for values in [list(range(1000)), {'nested': nested}]:
        source = {'text': PADDING, 'values': values}
        assert object_arguments(json.dumps(source)) == source


def test_python_carrier_cycles_and_nonfinite_values_still_reject():
    cycle = {}; cycle['self'] = cycle
    for source in [cycle, {'value': float('inf')}, {'value': object()}]:
        with pytest.raises(ProtocolConversionError) as error:
            object_arguments(source)
        assert error.value.code == 'tool_arguments_invalid_json'


def test_injected_decoder_custom_carrier_keeps_encoder_failure(monkeypatch):
    class Unsupported: pass
    monkeypatch.setattr(json, 'loads', lambda *args, **kwargs: {'custom': Unsupported()})
    with pytest.raises(ProtocolConversionError) as error:
        object_arguments(' ' * 70000)
    assert error.value.code == 'tool_arguments_invalid_json'


def test_decoder_recursion_failure_stays_fixed_code(monkeypatch):
    def failed_decoder(*args, **kwargs):
        raise RecursionError('fixture decoder stack boundary')
    monkeypatch.setattr(json, 'loads', failed_decoder)
    with pytest.raises(ProtocolConversionError) as error:
        object_arguments(' ' * 70000)
    assert error.value.code == 'tool_arguments_invalid_json'


@pytest.mark.parametrize('encoded', ['1e999', '-1e999', '[]', 'null'])
def test_nonobject_shape_keeps_priority_over_float_overflow(encoded):
    with pytest.raises(ProtocolConversionError) as error:
        object_arguments(encoded)
    assert error.value.code == 'tool_arguments_object_required'


def test_custom_decoder_method_remains_encoder_validated(monkeypatch):
    def custom_decode(self, *args, **kwargs):
        return {'invalid': object()}
    monkeypatch.setattr(json.JSONDecoder, 'decode', custom_decode)
    with pytest.raises(ProtocolConversionError) as error:
        object_arguments('{}')
    assert error.value.code == 'tool_arguments_invalid_json'


def test_float_callback_stack_failure_retries_legacy_decoder(monkeypatch):
    from lib.llm import conversion
    def callback_stack_failure(_value):
        raise RecursionError('fixture callback boundary')
    monkeypatch.setattr(conversion.math, 'isfinite', callback_stack_failure)
    assert object_arguments('{"value":1.25}') == {'value': 1.25}
