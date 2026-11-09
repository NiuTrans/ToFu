"""A long-running process imports application modules from the source it pinned near boot."""
import importlib
import importlib.util
import os
import sys
import textwrap
import traceback
import uuid

import pytest

from lib.runtime.pinned_import_source import PinnedImportSource

pytestmark = pytest.mark.unit

PROVIDER_BOOT = 'def stable():\n    return "boot"\n'
PROVIDER_MERGED = PROVIDER_BOOT + 'def added_later():\n    return "merge"\n'
CONSUMER_BOOT = '''
def run():
    from {package}.provider import stable
    return stable()
'''
CONSUMER_MERGED = '''
def run():
    from {package}.provider import added_later, stable
    return stable() + added_later()
'''


class Checkout:
    """A synthetic application root on sys.path holding one uniquely named package."""

    def __init__(self, root, package):
        self.root = root
        self.package = package
        self.sources = []
        self.names = [package]

    def write(self, relative, text):
        path = self.root / self.package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).format(package=self.package))
        importlib.invalidate_caches()
        return path

    def load(self, name=''):
        return importlib.import_module(f'{self.package}.{name}' if name else self.package)

    def install(self):
        source = PinnedImportSource([self.root])
        source.install()
        self.sources.append(source)
        return source

    def pin(self):
        source = self.install()
        source.join()
        return source


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = tmp_path / 'checkout'
    package = f'pinned_app_{uuid.uuid4().hex}'
    (root / package).mkdir(parents=True)
    (root / package / '__init__.py').write_text('')
    monkeypatch.syspath_prepend(str(root))
    checkout = Checkout(root, package)
    yield checkout
    for source in checkout.sources:
        source.join()
        source.uninstall()
    for name in list(sys.modules):
        if name.partition('.')[0] in checkout.names:
            del sys.modules[name]


def _merge_provider_and_consumer(checkout):
    checkout.write('provider.py', PROVIDER_MERGED)
    checkout.write('consumer.py', CONSUMER_MERGED)


def test_control_checkout_import_mixes_revisions_without_a_pin(checkout):
    checkout.write('provider.py', PROVIDER_BOOT)
    checkout.write('consumer.py', CONSUMER_BOOT)
    checkout.load('provider')
    _merge_provider_and_consumer(checkout)
    consumer = checkout.load('consumer')
    with pytest.raises(ImportError, match="cannot import name 'added_later'"):
        consumer.run()


def test_lazy_import_after_a_merge_compiles_the_pinned_revision(checkout):
    checkout.write('provider.py', PROVIDER_BOOT)
    checkout.write('consumer.py', CONSUMER_BOOT)
    checkout.load('provider')
    checkout.pin()
    _merge_provider_and_consumer(checkout)
    assert checkout.load('consumer').run() == 'boot'


def test_package_first_imported_after_installation_is_pinned_at_that_import(checkout):
    checkout.write('helper.py', 'VALUE = "first import"\n')
    source = checkout.install()
    checkout.load()
    source.join()
    checkout.write('helper.py', 'VALUE = "rewritten by a later merge"\n')
    assert checkout.load('helper').VALUE == 'first import'


def test_root_module_imports_its_pinned_source(checkout):
    name = f'pinned_root_{uuid.uuid4().hex}'
    checkout.names.append(name)
    path = checkout.root / f'{name}.py'
    path.write_text('VALUE = "boot"\n')
    checkout.pin()
    path.write_text('VALUE = "rewritten by a later merge"\n')
    importlib.invalidate_caches()
    assert importlib.import_module(name).VALUE == 'boot'


def test_module_deleted_after_pinning_still_imports(checkout):
    path = checkout.write('nested/__init__.py', '')
    checkout.write('nested/helper.py', 'VALUE = "boot"\n')
    checkout.load()
    checkout.pin()
    (path.parent / 'helper.py').unlink()
    path.unlink()
    path.parent.rmdir()
    importlib.invalidate_caches()
    assert checkout.load('nested.helper').VALUE == 'boot'


def test_module_added_after_pinning_loads_from_the_checkout(checkout):
    checkout.load()
    checkout.pin()
    checkout.write('added.py', 'VALUE = "added later"\n')
    assert checkout.load('added').VALUE == 'added later'


def test_only_regular_package_directories_are_pinned(checkout):
    checkout.write('sub/__init__.py', '')
    checkout.write('sub/mod.py', 'VALUE = "boot"\n')
    checkout.write('runtime_data/note.py', 'VALUE = "boot"\n')
    checkout.load()
    checkout.pin()
    checkout.write('sub/mod.py', 'VALUE = "rewritten by a later merge"\n')
    checkout.write('runtime_data/note.py', 'VALUE = "rewritten at runtime"\n')
    assert checkout.load('sub.mod').VALUE == 'boot'
    # A directory without __init__.py is never listed, so its files stay live.
    assert checkout.load('runtime_data.note').VALUE == 'rewritten at runtime'


def test_modules_outside_the_application_roots_are_not_pinned(checkout, tmp_path, monkeypatch):
    other = tmp_path / 'site'
    other.mkdir()
    name = f'outside_{uuid.uuid4().hex}'
    checkout.names.append(name)
    (other / f'{name}.py').write_text('VALUE = "installed"\n')
    monkeypatch.syspath_prepend(str(other))
    checkout.pin()
    (other / f'{name}.py').write_text('VALUE = "upgraded in place"\n')
    importlib.invalidate_caches()
    assert importlib.import_module(name).VALUE == 'upgraded in place'


def test_reload_reads_the_current_checkout(checkout):
    checkout.write('registry.py', 'ROWS = ("boot",)\n')
    checkout.load()
    checkout.pin()
    registry = checkout.load('registry')
    checkout.write('registry.py', 'ROWS = ("boot", "edited")\n')
    assert importlib.reload(registry).ROWS == ('boot', 'edited')


def test_bytecode_is_cached_only_while_the_checkout_holds_the_pinned_revision(
        checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'dont_write_bytecode', False)
    monkeypatch.setattr(sys, 'pycache_prefix', str(tmp_path / 'bytecode'))
    unchanged = checkout.write('unchanged.py', 'VALUE = "boot"\n')
    rewritten = checkout.write('rewritten.py', 'VALUE = "boot"\n')
    checkout.load()
    checkout.pin()
    checkout.write('rewritten.py', 'VALUE = "rewritten by a later merge"\n')
    assert checkout.load('unchanged').VALUE == 'boot'
    assert checkout.load('rewritten').VALUE == 'boot'
    assert os.path.exists(importlib.util.cache_from_source(str(unchanged)))
    assert not os.path.exists(importlib.util.cache_from_source(str(rewritten)))


def test_tracebacks_show_the_pinned_lines_that_ran(checkout):
    checkout.write('failing.py', 'def fail():\n    raise ValueError("pinned line")\n')
    checkout.load()
    checkout.pin()
    checkout.write('failing.py', '# rewritten\n# by a later merge\ndef fail():\n    return None\n')
    with pytest.raises(ValueError) as caught:
        checkout.load('failing').fail()
    assert 'raise ValueError("pinned line")' in ''.join(traceback.format_exception(caught.value))


def test_frozen_generation_pins_unimported_packages_and_reload(checkout):
    checkout.write('helper.py', 'VALUE = "prepared"\n')
    source = checkout.install()
    identity = source.freeze()
    assert identity['sourceFiles'] == 2
    checkout.write('helper.py', 'VALUE = "later checkout"\n')
    module = checkout.load('helper')
    assert module.VALUE == 'prepared'
    assert importlib.reload(module).VALUE == 'prepared'


def test_frozen_generation_retains_deleted_top_level_package(checkout):
    helper = checkout.write('helper.py', 'VALUE = "prepared"\n')
    source = checkout.install()
    source.freeze()
    helper.unlink()
    (helper.parent / '__init__.py').unlink()
    importlib.invalidate_caches()
    assert checkout.load('helper').VALUE == 'prepared'


def test_frozen_generation_refuses_new_application_modules(checkout):
    source = checkout.install()
    source.freeze()
    checkout.write('added.py', 'VALUE = "not prepared"\n')
    with pytest.raises(ImportError, match='generation'):
        checkout.load('added')


@pytest.mark.parametrize('linked_environment', [False, True])
def test_frozen_application_does_not_own_an_installed_environment_under_its_root(
        checkout, monkeypatch, linked_environment):
    environment = checkout.root / '.venv'
    if linked_environment:
        target = checkout.root.parent / 'shared-environment'
        target.mkdir()
        environment.symlink_to(target, target_is_directory=True)
    packages = environment / 'lib' / 'python3' / 'site-packages'
    dependency_name = 'installed_' + checkout.package
    dependency = packages / dependency_name
    dependency.mkdir(parents=True)
    (dependency / '__init__.py').write_text('VALUE = "installed dependency"\n')
    (dependency / 'helper.py').write_text('VALUE = "lazy installed module"\n')
    monkeypatch.syspath_prepend(str(packages))
    checkout.names.append(dependency_name)
    source = checkout.install()
    identity = source.freeze()

    assert importlib.import_module(dependency_name).VALUE == 'installed dependency'
    assert importlib.import_module(dependency_name + '.helper').VALUE == 'lazy installed module'
    assert source.freeze() == identity


def test_frozen_generation_still_refuses_new_nested_application_packages(checkout):
    source = checkout.install()
    source.freeze()
    checkout.write('new_package/__init__.py', 'VALUE = "new checkout"\n')
    with pytest.raises(ImportError, match='generation'):
        checkout.load('new_package')


def test_frozen_generation_requires_every_source_to_be_read(checkout, monkeypatch):
    import lib.runtime.pinned_import_source as pins
    checkout.write('helper.py', 'VALUE = "prepared"\n')
    source = checkout.install()
    source.join()
    original = pins._read_pin
    monkeypatch.setattr(pins, '_read_pin', lambda path: None if path.endswith('helper.py') else original(path))
    with pytest.raises(RuntimeError, match='pin'):
        source.freeze()


def test_frozen_identity_describes_actual_pins_not_later_checkout(checkout):
    from serverctl_pkg.execution_plan import prepare_plan
    checkout.write('helper.py', 'VALUE = "before"\n')
    checkout.load()
    source = checkout.pin()
    before = prepare_plan(checkout.root, {})
    checkout.write('helper.py', 'VALUE = "after"\n')
    identity = source.freeze()
    assert identity['sourceSha256'] == before['sourceSha256']
    assert identity['sourceSha256'] != prepare_plan(checkout.root, {})['sourceSha256']
