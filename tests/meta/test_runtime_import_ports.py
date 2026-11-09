"""Raw section harnesses inject exactly the owners needed by their bindings."""
import json

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


def test_section_composition_excludes_type_only_bindings(tmp_path, monkeypatch):
    """A manifest's TypeScript-only import cannot become a browser global."""
    owner = tmp_path / 'owner.js'
    monkeypatch.setattr(runtime, '_section_import_ports', lambda _name: (
        ((('type ContextSnapshot', 'ContextSnapshot'),), owner),
    ))
    monkeypatch.setattr(runtime, '_import_port_path', lambda module: str(module))
    result = runtime._compose_import_ports(
        'fixture.js', 'ContextSnapshot;')
    assert result == 'ContextSnapshot;'


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


def test_sections_share_prelude_resolution_and_revalidate_changed_text(tmp_path, monkeypatch):
    sections = tmp_path / 'sections'
    sections.mkdir()
    owner = tmp_path / 'owner.ts'
    other = tmp_path / 'other.ts'
    lazy = tmp_path / 'lazy.ts'
    for path in (owner, other, lazy):
        path.write_text('export const value = 1;\n')
    prelude = sections / 'prelude.ts'
    prelude.write_text("import { value as alias, type Context, makeService } from './owner';\n"
                       'const service = makeService();\n')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'prelude': prelude.name, 'lazyBundles': [{
        'sections': [{'source': 'lazy-fixture.js'}],
        'moduleImports': [{'source': lazy.name, 'bindings': ['value']}],
    }]}))
    monkeypatch.setattr(runtime, 'ROOT', tmp_path)
    monkeypatch.setattr(runtime, 'RUNTIME', tmp_path / 'runtime.js')
    monkeypatch.setattr(runtime, 'SECTIONS', sections)
    monkeypatch.setattr(runtime, 'MANIFEST', manifest)
    original = runtime._resolve_frontend_module
    resolutions = []

    def observed(origin, specifier):
        resolutions.append((origin, specifier))
        return original(origin, specifier)

    monkeypatch.setattr(runtime, '_resolve_frontend_module', observed)
    runtime._section_import_ports.cache_clear()
    try:
        expected = (((('alias', 'value'), ('makeService', 'makeService')), owner),
                    ('service', 'makeService'))
        assert runtime._section_import_ports('eager-fixture.js') == expected
        assert runtime._section_import_ports('lazy-fixture.js') == (
            ((('value', 'value'),), lazy), *expected)
        assert len(resolutions) == 1
        prelude.write_text("import { value as changed } from './other';\n")
        assert runtime._section_import_ports('changed-fixture.js') == (
            ((('changed', 'value'),), other),)
        assert len(resolutions) == 2
        relocated = tmp_path / 'relocated'
        relocated.mkdir()
        relocated_owner = relocated / other.name
        relocated_owner.write_bytes(other.read_bytes())
        monkeypatch.setattr(runtime, 'RUNTIME', relocated / 'runtime.js')
        assert runtime._section_import_ports('relocated-fixture.js') == (
            ((('changed', 'value'),), relocated_owner),)
        assert len(resolutions) == 3
    finally:
        runtime._section_import_ports.cache_clear()
        runtime._prelude_import_ports.cache_clear()
