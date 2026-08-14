"""Load side-effect-free data-layer leaves without application bootstrap."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_ROOT = Path(__file__).resolve().parents[1]
_CACHE = {}

# Direct ``python scripts/tool.py`` execution puts only ``scripts/`` on
# sys.path.  Data-layer leaves may import harmless shared modules such as
# lib.log; expose the project root without importing ``lib.database``.
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def load_database_leaf(name: str):
    if not name or '/' in name or '\\' in name or name.startswith('.'):
        raise ValueError(f'invalid database leaf name: {name!r}')
    if name in _CACHE:
        return _CACHE[name]
    path = _ROOT / 'lib' / 'database' / f'{name}.py'
    if not path.is_file():
        raise RuntimeError(f'database tooling leaf is missing: {path}')
    module_name = f'_tofu_database_tool_leaf_{name}'
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'cannot load database tooling leaf: {path}')
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    _CACHE[name] = module
    return module


__all__ = ['load_database_leaf']
