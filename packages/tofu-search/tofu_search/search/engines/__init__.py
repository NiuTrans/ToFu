"""tofu_search/search/engines/ — Individual search engine implementations.

The package loads every engine. A thread that first imports one engine while
this package is absent holds that engine's import lock while loading the
package, then waits for the ``tofu_search`` facade, which needs every engine;
the engine must already be complete by then (``tests/test_import_graph.py``).
"""

from importlib import import_module

import_module('tofu_search.search.engines.bing')
import_module('tofu_search.search.engines.brave')
import_module('tofu_search.search.engines.ddg')
import_module('tofu_search.search.engines.gateway')
import_module('tofu_search.search.engines.marginalia')
import_module('tofu_search.search.engines.searxng')
import_module('tofu_search.search.engines.xhs')
