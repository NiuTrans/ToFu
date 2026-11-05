"""Raw section harnesses inject exactly the owners needed by their bindings."""
import pytest

from tests import _runtime_sections as runtime

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('body,needed', [
    ('port();', True),
    ('object.port();', False),
    ('const port = () => {}; port();', False),
    ('const object = {port: value};', False),
    ('const object = {port};', True),
    ('function* port() {}', False),
    ('report(port);', True),
])
def test_section_composition_injects_free_ports_and_preserves_body(tmp_path, monkeypatch, body, needed):
    owner = tmp_path / 'owner.js'
    owner.write_text('globalThis.port = () => {};\n')
    monkeypatch.setattr(runtime, '_section_import_ports', lambda _name: (((('port', 'port'),), owner),))
    monkeypatch.setattr(runtime, '_import_port_path', lambda module: str(module))
    result = runtime._compose_import_ports('fixture.js', body)
    assert result == (owner.read_text() if needed else '') + body


def test_section_service_uses_its_factory_even_when_only_service_is_referenced(tmp_path, monkeypatch):
    owner = tmp_path / 'owner.js'
    owner.write_text('globalThis.createService = () => ({});\n')
    monkeypatch.setattr(runtime, '_section_import_ports', lambda _name: (
        ((('createService', 'createService'),), owner), ('service', 'createService')))
    monkeypatch.setattr(runtime, '_import_port_path', lambda module: str(module))
    result = runtime._compose_import_ports('fixture.js', 'service.render();')
    assert result.startswith(owner.read_text())
    assert "globalThis['service'] = createService();" in result
    assert result.endswith('service.render();')
