# Contributing to ChatUI

Thank you for contributing! This guide covers coding conventions, log-level
policy, design patterns, and the pre-submit checklist. Please read it before
opening a PR.

---

## Table of Contents

- [Module Architecture Overview](#module-architecture-overview)
  - [Package Map](#package-map)
  - [Request Lifecycle](#request-lifecycle)
- [Log Level Decision Guide](#log-level-decision-guide)
  - [Decision Flowchart](#decision-flowchart)
  - [Level Definitions](#level-definitions)
  - [Hot-Path Rules](#hot-path-rules)
  - [Traceback Rules](#traceback-rules-exc_infotrue)
  - [Log Infrastructure](#log-infrastructure)
- [Package Façade Pattern](#package-façade-pattern)
  - [Principles](#principles)
  - [Anatomy of a Façade](#anatomy-of-a-façade)
  - [Core vs. Optional Sub-modules](#core-vs-optional-sub-modules)
  - [The `lib/_pkg_utils.py` Toolkit](#the-lib_pkg_utilspy-toolkit)
  - [Adding a New Package](#adding-a-new-package)
  - [Lazy-Loading Variant (`lib/tasks_pkg`)](#lazy-loading-variant-libtasks_pkg)
  - [CI Validation](#ci-validation)
- [Protocol Pattern for Module Boundaries](#protocol-pattern-for-module-boundaries)
  - [Why Protocols](#why-protocols)
  - [Protocol Catalogue](#protocol-catalogue)
  - [Using Protocols in Consumer Code](#using-protocols-in-consumer-code)
  - [Adding a New Protocol](#adding-a-new-protocol)
- [Design Patterns in Use](#design-patterns-in-use)
  - [ToolRegistry](#toolregistry-libtasks_pkgexecutorpy)
  - [TradingClient DI](#tradingclient-di-libtrading_commonpy)
  - [Module-Level Dispatch Table](#module-level-dispatch-table-libtasks_pkgtool_displaypy)
- [Bug Prevention Checklist](#bug-prevention-checklist)

---

## Module Architecture Overview

### Package Map

ChatUI's backend is organized into **13 decomposed packages** under `lib/`,
each using the [Package Façade Pattern](#package-façade-pattern). The
architecture separates **request-handling hot-path** modules from
**domain-specific computation** packages.

```
lib/
├── __init__.py              ← Root config (API keys, model names, env vars)
├── _pkg_utils.py            ← Shared façade utilities (build_facade, safe_import, …)
├── protocols.py             ← Protocol interfaces for all module boundaries
├── log.py                   ← Centralized logging (get_logger, log_context, audit_log, …)
├── llm_client.py            ← LLM API communication (body building, SSE parsing) [HOT_PATH]
├── search.py                ← Web search execution [HOT_PATH]
│
├── tasks_pkg/               ← Task orchestration system
│   ├── orchestrator.py      ← Main LLM↔tool loop [HOT_PATH]
│   ├── executor.py          ← Tool dispatch registry [HOT_PATH]
│   ├── tool_dispatch.py     ← Tool parsing + parallel execution [HOT_PATH]
│   ├── compaction.py        ← Context window compaction [HOT_PATH]
│   ├── model_config.py      ← Model config lookup [HOT_PATH]
│   ├── manager.py           ← Task CRUD, SSE events, persistence
│   ├── endpoint.py          ← Autonomous work→review→revise loop
│   └── ...
│
├── llm_dispatch/            ← Multi-key, multi-model LLM dispatcher
│   ├── slot.py              ← Slot dataclass (api_key × model)
│   ├── config.py            ← Default slot configurations
│   ├── dispatcher.py        ← LLMDispatcher (pool management)
│   ├── factory.py           ← DispatcherFactory + get_dispatcher()
│   └── api.py               ← High-level: dispatch_chat, smart_chat
│
├── fetch/                   ← Content fetching + extraction
│   ├── utils.py             ← HTTP session, user-agent rotation
│   ├── http.py              ← Raw HTTP + retry logic
│   ├── html_extract.py      ← Readability-based text extraction
│   ├── pdf_extract.py       ← PDF text extraction
│   ├── playwright_pool.py   ← Browser pool (optional dep)
│   └── core.py              ← Main entry point: fetch_page_content [HOT_PATH]
│
├── trading/                    ← Trading portfolio advisor
│   ├── _common.py           ← TradingClient (shared HTTP session, proxy)
│   ├── nav.py               ← NAV fetching / caching / history
│   ├── info.py              ← Fund info, search, fee calculation
│   ├── strategy_data.py     ← Built-in strategy definitions
│   ├── intel.py             ← Intelligence CRUD + crawling (optional)
│   └── backtest.py          ← Backtesting (optional)
│
├── trading_strategy_engine/    ← Pure-computation strategy engine (no LLM)
│   ├── strategy.py          ← Strategy Protocol/ABC, registry
│   ├── signals.py           ← Multi-timeframe signal confirmation
│   ├── risk_metrics.py      ← Risk-adjusted performance metrics
│   ├── ensemble.py          ← Ensemble strategy backtesting (optional)
│   ├── monte_carlo.py       ← Monte Carlo simulation (optional)
│   ├── optimization.py      ← Walk-forward optimization (optional)
│   ├── portfolio.py         ← Portfolio construction (optional)
│   └── pipeline.py          ← Full analysis pipeline (optional)
│
├── trading_backtest_engine/    ← Event-driven backtesting engine
│   ├── config.py            ← Default constants
│   ├── state.py             ← Portfolio state management
│   ├── strategies.py        ← Strategy implementations
│   ├── reporting.py         ← Metrics computation
│   ├── engine.py            ← Core simulation engine
│   ├── validation.py        ← Walk-forward validation (optional)
│   ├── comparison.py        ← Multi-strategy comparison (optional)
│   └── analysis.py          ← Bias verification (optional)
│
├── trading_autopilot/          ← Autonomous trading analyst robot
│   ├── _constants.py        ← Thresholds & timing
│   ├── correlation.py       ← Intelligence cross-correlation (optional)
│   ├── strategy_evolution.py← Strategy self-improvement (optional)
│   ├── kpi.py               ← Pre-backtest scoring (optional)
│   ├── reasoning.py         ← LLM mega-prompt builder (optional)
│   ├── cycle.py             ← Full autopilot cycle runner (optional)
│   ├── scheduler.py         ← Periodic toggle (optional)
│   └── outcome.py           ← Recommendation outcome tracker (optional)
│
├── tools/                   ← Tool execution definitions
│   ├── search.py            ← web_search, site_search
│   ├── browser.py           ← Browser automation tools
│   ├── code_exec.py         ← Code execution tools
│   ├── conversation.py      ← Conversation reference tools
│   ├── meta.py              ← Dynamic tool list assembly
│   └── project.py           ← Project-mode file tools
│
├── swarm/                   ← Multi-agent orchestration
│   ├── artifact_store.py    ← Inter-agent data sharing [HOT_PATH]
│   └── ...
│
├── scheduler/               ← Task scheduler
│   ├── cron.py              ← Cron expression parser
│   ├── executor.py          ← Task execution handlers
│   ├── manager.py           ← SchedulerManager class
│   └── tool_defs.py         ← Scheduler tool schemas
│
└── feishu/                  ← Feishu (Lark) Bot integration
    ├── _state.py            ← Per-user state, locks, config
    ├── conversation.py      ← Chat history, DB sync (optional)
    ├── messaging.py         ← Lark API message sending (optional)
    ├── pipeline.py          ← LLM task pipeline (optional)
    ├── commands.py          ← Slash command registry (optional)
    ├── events.py            ← Event handlers (optional)
    └── startup.py           ← WebSocket connection (optional)
```

### Request Lifecycle

A user message flows through the hot-path modules in this order:

```
Client POST /api/chat/start
       │
       ▼
 ┌─ orchestrator.py ─────────────────────────────────────┐
 │  1. _resolve_model_config()    ← model_config.py      │
 │  2. _inject_system_contexts()  ← system_context.py    │
 │  3. _prefetch_user_urls()      ← executor.py          │
 │  4. ┌─ TOOL LOOP ───────────────────────────────────┐ │
 │     │ a. build_body()          ← llm_client.py       │ │
 │     │ b. stream_llm_response() ← manager.py          │ │
 │     │ c. parse_tool_calls()    ← tool_dispatch.py    │ │
 │     │ d. execute_tool_pipeline()                      │ │
 │     │    └─ _execute_tool_one() ← executor.py        │ │
 │     │       ├─ perform_web_search() ← search.py      │ │
 │     │       ├─ fetch_page_content() ← fetch/core.py  │ │
 │     │       └─ [other tool handlers]                  │ │
 │     │ e. compact_messages_fast() ← compaction.py     │ │
 │     └─ REPEAT until model says "stop" ───────────────┘ │
 │  5. persist_task_result()       ← manager.py           │
 └────────────────────────────────────────────────────────┘
       │
       ▼
 Client polls /api/chat/stream/<id> for SSE events
```

Every module in this chain is marked `# HOT_PATH` and must follow the
[Hot-Path Rules](#hot-path-rules).

---

## Log Level Decision Guide

Use this table to select the correct log level for new code. Misuse of log
levels pollutes production logs and obscures real issues.

### Decision Flowchart

```
Is this a per-request / per-item event?
  YES → Is it useful ONLY during active debugging?
    YES → logger.debug(...)
    NO  → Is it a recoverable/expected failure?
      YES → logger.warning(...)  [no exc_info unless traceback adds value]
      NO  → logger.error(..., exc_info=True)
  NO → Is it a one-time startup / config event?
    YES → logger.info(...)
    NO  → Is it a periodic background event (e.g. scheduled task)?
      YES → logger.info(...)  [at most once per cycle]
      NO  → logger.debug(...)
```

### Level Definitions

| Level      | When to Use                                                       | Example                                                    |
|------------|-------------------------------------------------------------------|------------------------------------------------------------|
| `DEBUG`    | Per-request traces, tool arguments, retry attempts, individual trading data fetches, loop iterations, parsed values, cache hits/misses | `logger.debug('Fetched %d bytes from %s', n, url)`         |
| `INFO`     | Server startup, config changes, new conversation created, scheduled task registered, periodic summaries (1/cycle) | `logger.info('Server started on port %d', port)`           |
| `WARNING`  | Recoverable errors (network retry succeeded), degraded mode, missing optional deps, fallback triggered, resilient-import failures | `logger.warning('Proxy failed, retrying direct')`          |
| `ERROR`    | Unrecoverable failures requiring attention — LLM call failed all retries, DB corruption, critical service unavailable; **always** add traceback | `logger.error('DB write failed', exc_info=True)`           |
| `CRITICAL` | System-level failures preventing continued operation              | `logger.critical('Cannot open database')`                  |

#### Quick-Reference Decision Table

| Scenario                                    | Level     | `exc_info` | Why                                                    |
|---------------------------------------------|-----------|------------|--------------------------------------------------------|
| Logging each tool argument before dispatch  | `DEBUG`   | No         | Per-request detail, only useful when debugging         |
| Individual asset price fetch from API          | `DEBUG`   | No         | Per-item data retrieval, high frequency                |
| Retry attempt N of M for an HTTP request    | `DEBUG`   | No         | Per-attempt trace; the final outcome uses WARNING/ERROR|
| Cache hit on fetch URL                      | `DEBUG`   | No         | High frequency, operational detail only                |
| LLM body construction details               | `DEBUG`   | No         | Hot-path, per-request                                  |
| SSE chunk parsed from LLM stream            | `DEBUG`   | No         | Very high frequency, never useful except deep debugging|
| Flask server started on port 5001           | `INFO`    | No         | One-time startup event                                 |
| New conversation created                    | `INFO`    | No         | Significant state change, low frequency                |
| Scheduled task registered                   | `INFO`    | No         | One-time lifecycle event                               |
| Config value changed via admin API          | `INFO`    | No         | Operational event worth auditing                       |
| Autopilot cycle started/completed           | `INFO`    | No         | Periodic summary, once per cycle                       |
| Network request failed but retry succeeded  | `WARNING` | No         | Recovered — no action needed, but worth noting         |
| Optional dependency missing (e.g. playwright)| `WARNING`| `True`     | Feature degraded but system operational                |
| Resilient import failed in façade           | `WARNING` | `True`     | Package continues without this sub-module              |
| Running in degraded mode (no proxy)         | `WARNING` | No         | Operator should be aware of reduced capability         |
| JSON parse error on user data (recovered)   | `WARNING` | `True`     | Traceback helps diagnose malformed data                |
| LLM API call failed after all retries       | `ERROR`   | `True`     | Unrecoverable — operator needs to investigate          |
| Database corruption detected                | `ERROR`   | `True`     | Data integrity issue requiring attention               |
| Critical external service unreachable       | `ERROR`   | `True`     | System cannot fulfil core function                     |
| Max tool rounds exhausted (infinite loop?)  | `WARNING` | No         | Operational issue but task completes with partial result|
| `finish_reason` is `None` from LLM         | `ERROR`   | No         | Stream likely interrupted — needs investigation        |

#### Formatting Rules

- **Always use %-style formatting** (lazy evaluation):
  ```python
  # ✅ Correct — args only formatted if level is enabled
  logger.info('Fetched %s in %.1fs', url, elapsed)

  # ❌ Wrong — f-string always evaluates, wastes CPU on hot path
  logger.info(f'Fetched {url} in {elapsed:.1f}s')
  ```
- **Include identifiers for grep-ability**: task ID, conversation ID, model name, URL.
- **Truncate large data**: `logger.debug('Response preview: %.500s', body)`
- **Sanitize secrets**: never log API keys, tokens, or full auth headers.
- **Structured prefix** for easy grepping: `[LLM]`, `[ext:service]`, `[op:name]`, `[rid:xxxx]`.

### Hot-Path Rules

**Convention:** Any module whose functions are called on the per-request path
MUST include a module-level comment `# HOT_PATH` as the **very first line** of
the file (before the module docstring). This is machine-grep-able and keeps the
list of hot-path modules self-documenting.

```python
# HOT_PATH — functions in this module are called per-request.
# Do NOT use logger.info() here; use logger.debug() instead.
"""Tool execution — unified dispatch for all tool types."""

from __future__ import annotations
...
```

**Discovering hot-path modules** — grep, don't maintain a hardcoded list:

```bash
# List all hot-path modules
grep -rln '# HOT_PATH' lib/ | sort
```

**CI enforcement** — fail the build if any `# HOT_PATH` module uses `logger.info()`:

```bash
#!/usr/bin/env bash
hot_files=$(grep -rln '# HOT_PATH' lib/)
if [ -n "$hot_files" ]; then
  echo "$hot_files" | xargs grep -n 'logger\.info' \
    && { echo "FAIL: logger.info() found in HOT_PATH module(s)"; exit 1; }
fi
```

> **Rule of thumb:** if you are inside a `# HOT_PATH` module, always reach
> for `logger.debug()`. Use `logger.info()` only for true one-time events
> (server boot, config load) — and those belong in non-hot-path modules.

#### Known Hot-Path Modules (reference)

The canonical list is always `grep -rln '# HOT_PATH' lib/`, but these are
the modules known at time of writing:

| Module                              | Role in Request Path                                |
|-------------------------------------|-----------------------------------------------------|
| `lib/tasks_pkg/orchestrator.py`    | Main LLM↔tool loop — entry point per request        |
| `lib/tasks_pkg/executor.py`        | Tool execution dispatch — called per tool call       |
| `lib/tasks_pkg/tool_dispatch.py`   | Tool argument parsing + parallel execution           |
| `lib/tasks_pkg/compaction.py`      | Context compaction — called during long conversations|
| `lib/tasks_pkg/model_config.py`    | Model config lookup — called per LLM request         |
| `lib/llm_client.py`                | LLM API communication — called per LLM request      |
| `lib/search.py`                    | Search execution — called per search tool invocation |
| `lib/fetch/core.py`                | Content fetching — called per fetch_url tool call    |
| `lib/swarm/artifact_store.py`      | Inter-agent artifact I/O — called per agent step     |

If you add a new module to the per-request path, add `# HOT_PATH` at the top.

### Traceback Rules (`exc_info=True`)

- **Always** add `exc_info=True` to `logger.error()` calls inside `except`
  blocks — the traceback is the single most valuable piece of debugging
  information. No exceptions to this rule.

  ```python
  # ✅ CORRECT — error in except block always gets exc_info
  try:
      data = complex_operation()
  except Exception as e:
      logger.error('complex_operation failed: %s', e, exc_info=True)
      raise
  ```

- **Add** `exc_info=True` to `logger.warning()` when the exception itself is
  the primary news (e.g., unexpected parse failure where the traceback helps
  diagnosis, or a resilient-import failure in a façade).

  ```python
  # ✅ When the traceback adds diagnostic value
  except json.JSONDecodeError as e:
      logger.warning('Unexpected JSON in DB row %s: %s', row_id, e, exc_info=True)
  ```

- **Omit** `exc_info=True` from `logger.warning()` when recovery succeeded
  and the warning just notes the fallback (e.g., "Retried with backup model").

  ```python
  # ✅ Recovered — traceback would be noise
  except ConnectionError:
      logger.warning('Primary API down, falling back to backup endpoint')
  ```

- **Never** add `exc_info=True` to `logger.debug()` — tracebacks in debug
  logs create noise without benefit.

### Log Infrastructure

The project uses `lib/log.py` as the centralized logging module. Every Python
file must use it:

```python
from lib.log import get_logger
logger = get_logger(__name__)
```

**Key utilities** provided by `lib/log.py`:

| Utility           | Purpose                                              | When to Use                           |
|-------------------|------------------------------------------------------|---------------------------------------|
| `get_logger(name)`| Get a named logger (usually `__name__`)              | Every module — top-level constant     |
| `log_exception()` | `logger.error(msg, exc_info=True)` shorthand         | Catch-and-reraise blocks              |
| `log_context(op)` | Context manager: logs start/end/duration/exception   | Any operation > 1 second              |
| `log_external()`  | Context manager for external API calls with timing   | HTTP calls to third-party services    |
| `log_route()`     | Decorator for Flask route handlers                   | All route handler functions           |
| `log_suppressed()`| Log a swallowed exception with context               | Guard clauses with `except: pass`     |
| `audit_log()`     | Structured JSON audit entry to `logs/audit.log`      | Security events, config changes       |
| `set_req_id()`    | Set per-request correlation ID (middleware)           | Called by request middleware           |
| `req_id()`        | Get current request ID                               | Prefix logs for correlation           |

**Log file layout** (configured in `server.py`):

| File              | Content                                     | Rotation            |
|-------------------|---------------------------------------------|---------------------|
| `logs/app.log`    | Business logic (lib.*, routes.*, server) INFO+ | Daily, 30 days    |
| `logs/access.log` | HTTP request log (werkzeug) INFO+           | Daily, 14 days      |
| `logs/error.log`  | WARNING/ERROR/CRITICAL from all sources     | 5 MB × 10 backups   |
| `logs/vendor.log` | Third-party libraries WARNING+ only         | 5 MB × 3 backups    |
| `logs/audit.log`  | Structured JSON audit trail                 | Append-only         |

---

## Package Façade Pattern

When decomposing a monolith `lib/X.py` into `lib/X/` (a package with
sub-modules), follow these conventions to maintain a clean public API.

### Principles

1. **`lib/X/__init__.py` is the façade** — consumers import from `lib.X`, never
   from `lib.X.submodule` directly.
2. Each sub-module defines `__all__` listing its public API.
3. The façade uses helpers from `lib/_pkg_utils.py` for consistency and
   resilient import handling.
4. **Core** sub-modules (required for basic operation) import directly —
   failures propagate immediately.
5. **Optional** sub-modules (features that can be disabled) are wrapped in
   `try/except` — failures are logged as warnings and the rest of the package
   continues to work.

### Anatomy of a Façade

Every package `__init__.py` under `lib/` follows the same structure:

```python
"""lib/X/ — Package description (Package Façade)

Sub-modules:
  core           — Main functionality
  config         — Configuration constants
  optional_feat  — Optional feature (graceful degradation)
"""

import logging
from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core modules (always required — let exceptions propagate) ────
from . import core, config  # noqa: E402
from .core import *          # noqa: F401,F403
from .config import *        # noqa: F401,F403

build_facade(__all__, core, config)

# ── Optional modules (graceful degradation) ──────────────────────
try:
    from . import optional_feat  # noqa: E402
    from .optional_feat import *  # noqa: F401,F403
    build_facade(__all__, optional_feat)
except Exception as _exc:
    _logger.warning('lib.X.optional_feat failed to load — feature disabled: %s',
                    _exc, exc_info=True)
```

**What `build_facade()` does:** reads each module's `__all__` and appends
those names to the package-level `__all__`. Combined with the `from .X import *`
statements, this ensures consumers can do `from lib.X import any_public_name`.

### Core vs. Optional Sub-modules

| Category     | Import Style                         | On Failure                           | Use When                                    |
|-------------|--------------------------------------|--------------------------------------|---------------------------------------------|
| **Core**    | Direct `from .X import *`            | Exception propagates → package fails | Sub-module is essential (e.g. NAV fetching) |
| **Optional**| `try/except` around `from .X import *`| Warning logged → package continues   | Feature can be disabled (e.g. Playwright)   |

**Real examples from the codebase:**

| Package                  | Core Sub-modules                              | Optional Sub-modules                                      |
|--------------------------|-----------------------------------------------|-----------------------------------------------------------|
| `lib/trading`               | `_common`, `nav`, `info`, `strategy_data`     | `intel`, `backtest`                                       |
| `lib/fetch`              | `utils`, `http`, `html_extract`, `pdf_extract`, `core` | `playwright_pool`                                |
| `lib/trading_strategy_engine`| `strategy`, `signals`, `risk_metrics`        | `ensemble`, `monte_carlo`, `optimization`, `portfolio`, `pipeline` |
| `lib/trading_backtest_engine`| `config`, `state`, `strategies`, `reporting`, `engine` | `validation`, `comparison`, `analysis`          |
| `lib/trading_autopilot`     | `_constants`                                  | `correlation`, `strategy_evolution`, `kpi`, `reasoning`, `cycle`, `scheduler`, `outcome` |
| `lib/llm_dispatch`       | *(none — all guarded)*                        | `config`, `slot`, `factory`, `dispatcher`, `api`          |
| `lib/feishu`             | `_state`                                      | `conversation`, `messaging`, `pipeline`, `commands`, `events`, `startup` |
| `lib/tools`              | All: `search`, `browser`, `code_exec`, `conversation`, `meta`, `project` | *(none)* |
| `lib/scheduler`          | All: `cron`, `executor`, `manager`, `tool_defs` | *(none)*                                               |

### The `lib/_pkg_utils.py` Toolkit

| Function           | Purpose                                                                 | Use Case                                 |
|--------------------|-------------------------------------------------------------------------|------------------------------------------|
| `build_facade()`   | Extends package `__all__` from multiple modules' `__all__` lists        | After core `from .X import *` statements |
| `safe_star_import()`| Programmatic `from module import *` into caller's namespace + `__all__`| When you need finer control              |
| `extend_all()`     | Appends one module's `__all__` to package `__all__`                     | Single module, `__all__` only (no import)|
| `resilient_import()`| `importlib.import_module` + `safe_star_import` in try/except           | Importing by fully-qualified name        |
| `safe_import()`    | Returns a `_SafeImporter` callable bound to a specific package          | Most concise pattern for optional imports|
| `facade_imports()` | Batch-imports multiple optional sub-modules in one call                  | Many optional modules at once            |
| `validate_all()`   | CI helper — checks all names in `__all__` actually exist in namespace   | Pre-submit CI check                      |

**Concise alternative pattern** using `safe_import()`:

```python
from lib._pkg_utils import build_facade, safe_import

__all__: list[str] = []
_import = safe_import(__name__, globals(), __all__)

# Core (must load)
from . import core, config
from .core import *
from .config import *
build_facade(__all__, core, config)

# Optional — one line each, independent (one failure doesn't block others)
_import('optional_a', 'feature A')
_import('optional_b', 'feature B')
```

**Batch pattern** using `facade_imports()`:

```python
from lib._pkg_utils import build_facade, facade_imports

__all__: list[str] = []

from . import core
from .core import *
build_facade(__all__, core)

# Optional — all in one call
facade_imports(__name__, globals(), __all__, [
    ('optional_a', 'feature A description'),
    ('optional_b', 'feature B description'),
])
```

### Adding a New Package

When decomposing `lib/monolith.py` into `lib/monolith/`:

1. **Create the directory** with sub-modules, each defining `__all__`.
2. **Create `__init__.py`** following the façade pattern above.
3. **Classify sub-modules** as core (direct import) or optional (try/except).
4. **Use `build_facade()`** to aggregate `__all__` lists.
5. **Test imports**: `python -c "from lib.monolith import *; print(__all__)"` — 
   the public API should be identical to the old monolith.
6. **Keep consumer imports unchanged**: `from lib.monolith import func` must
   still work.

### Lazy-Loading Variant (`lib/tasks_pkg`)

`lib/tasks_pkg` uses a different pattern optimized for startup time: only the
lightweight `manager` module is imported eagerly (it's used by route
registration at import time). Heavy modules like `orchestrator`, `executor`,
and `endpoint` are loaded on first access via `__getattr__`:

```python
# lib/tasks_pkg/__init__.py
__all__ = ['tasks', 'create_task', 'run_task', ...]

# Eager — lightweight, needed at import time
from lib.tasks_pkg.manager import tasks, create_task, append_event, ...

# Lazy — loaded on first access
_LAZY_MAP = {
    'run_task':        ('lib.tasks_pkg.orchestrator', 'run_task'),
    'ToolRegistry':    ('lib.tasks_pkg.executor',     'ToolRegistry'),
    'run_endpoint_task':('lib.tasks_pkg.endpoint',    'run_endpoint_task'),
    ...
}

def __getattr__(name):
    if name in _LAZY_MAP:
        module_path, attr_name = _LAZY_MAP[name]
        import importlib
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr_name)
        globals()[name] = val  # Cache — __getattr__ only called once
        return val
    raise AttributeError(...)
```

Use this variant when a package has both:
- Symbols needed at import time (e.g., task dict, locks)
- Heavy modules that can be deferred (e.g., executor with many deps)

### CI Validation

Verify façade completeness in CI:

```python
from lib._pkg_utils import validate_all
import lib.tools

missing = validate_all(lib.tools)
assert not missing, f"Stale __all__ entries: {missing}"
```

---

## Protocol Pattern for Module Boundaries

### Why Protocols

The project uses `typing.Protocol` (PEP 544) for **structural subtyping** at
high-coupling module boundaries. This enables:

1. **No hard imports** — consumers don't import the concrete module → avoids
   circular imports, reduces coupling.
2. **Testability** — mock objects satisfying the protocol can be injected
   without monkeypatching module globals.
3. **Documented contracts** — the expected interface is explicit in type
   annotations, not implicit in "I happen to call these methods."

All protocols live in **one file**: `lib/protocols.py`. This is deliberate —
it's the single location for all boundary contracts, importable by any module
without pulling in transitive dependencies.

### Protocol Catalogue

| Protocol           | Methods                                    | Satisfied By                        | Used By                                  |
|--------------------|--------------------------------------------|-------------------------------------|------------------------------------------|
| `LLMService`       | `chat()`, `stream()`                       | `lib.llm_dispatch` module functions | trading_autopilot, swarm, tasks_pkg, trading.intel |
| `FetchService`     | `fetch_page_content()`, `fetch_urls()`     | `lib.fetch` module functions        | tasks_pkg.executor, trading.intel           |
| `TradingDataProvider` | `get_latest_price()`, `fetch_asset_info()`, `fetch_price_history()`, `build_intel_context()` | `lib.trading` module functions | trading_autopilot |
| `TaskEventSink`    | `append_event()`                           | `lib.tasks_pkg.manager.append_event`| executor, tool_dispatch                  |
| `ToolHandler`      | `__call__(task, tc, fn_name, …)`           | Each `@tool_registry.handler()` fn  | ToolRegistry dispatch                    |
| `BodyBuilder`      | `__call__(model, messages, …)`             | `lib.llm_client.build_body`         | swarm agents, orchestrators              |

All protocols are `@runtime_checkable` — you can use `isinstance()` checks
in defensive code.

### Using Protocols in Consumer Code

**Type annotation at function boundaries:**

```python
from lib.protocols import LLMService

def my_function(llm: LLMService, prompt: str) -> str:
    """Uses structural subtyping — any object with .chat() works."""
    content, usage = llm.chat(
        [{'role': 'user', 'content': prompt}],
        max_tokens=1024,
    )
    return content
```

**Dependency injection with fallback to concrete import:**

```python
from lib.protocols import FetchService

def _prefetch_user_urls(
    messages: list[dict],
    task: dict,
    *,
    fetch_service: FetchService | None = None,  # inject for testing
) -> list[tuple[str, str]]:
    # Fall back to concrete import when no DI provided
    if fetch_service:
        _fetch_urls = fetch_service.fetch_urls
    else:
        from lib.fetch import fetch_urls
        _fetch_urls = fetch_urls
    ...
```

**Explicit protocol binding in hot-path modules:**

```python
# lib/tasks_pkg/orchestrator.py
from lib.llm_client import build_body as _build_body_impl
from lib.protocols import BodyBuilder

build_body: BodyBuilder = _build_body_impl  # explicit protocol binding
```

This pattern serves as **living documentation**: readers immediately see that
`build_body` satisfies the `BodyBuilder` protocol, and type checkers can
verify it.

**Runtime type checking (defensive code):**

```python
from lib.protocols import LLMService

assert isinstance(llm_client, LLMService), "Expected LLMService protocol"
```

### Adding a New Protocol

1. **Define it in `lib/protocols.py`** — the single location for all boundary
   contracts.
2. **Use `@runtime_checkable`** so it works with `isinstance()`.
3. **Document in the docstring** which concrete types satisfy the protocol.
4. **Add to `__all__`** in `lib/protocols.py`.
5. **Add to the table above** in this document.

**Template:**

```python
@runtime_checkable
class MyServiceProtocol(Protocol):
    """Protocol for [description].

    Satisfied by:
      - ``lib.my_module`` module-level functions
      - Test mocks

    Used by:
      - ``lib.consumer_a``
      - ``lib.consumer_b``
    """

    def do_thing(self, arg: str) -> Result:
        """[Method description]."""
        ...
```

**When to create a protocol** — use this decision tree:

```
Does module A import concrete symbols from module B?
  YES → Do multiple modules (A, C, D) import the same symbols from B?
    YES → Is B heavy or slow to import?
      YES → Define a Protocol. ✅
      NO  → Is there a circular import risk?
        YES → Define a Protocol. ✅
        NO  → Do you want to mock B in tests for A?
          YES → Define a Protocol. ✅
          NO  → Direct import is fine. Skip Protocol.
    NO  → Direct import is fine. Skip Protocol.
```

---

## Design Patterns in Use

### ToolRegistry (`lib/tasks_pkg/executor.py`)

**Pattern:** Decorator-based dispatch registry for tool handlers.

The `ToolRegistry` class provides a central, extensible dispatch mechanism.
Tool handlers are registered at module-load time via decorators, and lookup is
performed at dispatch time via `registry.lookup(fn_name, round_entry)`.

**Three registration modes:**

| Mode          | Method / Decorator       | Use Case                                       |
|---------------|--------------------------|------------------------------------------------|
| **Exact**     | `@registry.handler(name)`| Single tool name → handler (O(1) dict lookup)  |
| **Set-based** | `@registry.tool_set(set)`| A group of related tools → one handler          |
| **Special**   | `@registry.special(key)` | Matched via `round_entry` metadata, not name    |

**Example — registering a handler:**

```python
from lib.tasks_pkg.executor import tool_registry

@tool_registry.handler('web_search', category='search',
                       description='Perform a web search via API')
def _handle_web_search(task, tc, fn_name, tc_id, fn_args, rn,
                       round_entry, cfg, project_path,
                       project_enabled, all_tools=None):
    ...
    return tc_id, tool_content, True  # (tc_id, content_str, is_search)

# Set-based: one handler for many tool names
@tool_registry.tool_set(BROWSER_TOOL_NAMES, category='browser',
                        description='Execute a browser automation tool')
def _handle_browser_tool(task, tc, fn_name, ...):
    ...

# Special: matched by round_entry metadata, not fn_name
@tool_registry.special('__code_exec__', category='code',
                       description='Execute a shell command')
def _handle_code_exec(task, tc, fn_name, ...):
    ...
```

**Lookup order:** exact match → special key check → set-based scan → `None`.

**Introspection:** `tool_registry.list_tools()` returns all registered tools
with metadata — useful for debugging and documentation generation.

### TradingClient DI (`lib/trading/_common.py`)

**Pattern:** Injectable HTTP client with lazy singleton initialization.

The `TradingClient` class encapsulates the HTTP session, proxy configuration, and
network-state logic. A module-level singleton is **lazily initialised** (not at
import time) via double-checked locking to avoid opening HTTP connections during
module import.

```python
class TradingClient:
    """HTTP client for trading data APIs with proxy support."""
    def __init__(self, proxy_url=None):
        self.session = requests.Session()
        self.proxy_url = proxy_url or os.environ.get('HTTPS_PROXY', '')
        ...

    def check_network(self) -> bool:
        """Thread-safe, rate-limited network health probe."""
        ...

# Lazy singleton with double-checked locking
_lazy_client = None
_lazy_lock = threading.Lock()

def _get_default_client() -> TradingClient:
    global _lazy_client
    if _lazy_client is not None:          # Fast path: no lock
        return _lazy_client
    with _lazy_lock:
        if _lazy_client is not None:      # Double-check after lock
            return _lazy_client
        _lazy_client = TradingClient()
    return _lazy_client
```

**Testing advantage:** Instantiate `TradingClient(proxy_url="")` directly in
tests — no monkeypatching of module globals required.

### Module-Level Dispatch Table (`lib/tasks_pkg/tool_display.py`)

**Pattern:** Static dict mapping tool names → display handlers, built once at
import time. Replaces long `if/elif` chains with O(1) dict lookup:

```python
_TOOL_DISPLAY_DISPATCH = _build_display_dispatch_table()  # built once at import
handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)
```

Similarly, `lib/tasks_pkg/executor.py` uses a browser badge dispatch table:

```python
_BROWSER_BADGE_DISPATCH = {
    'browser_list_tabs':   _badge_list_tabs,
    'browser_read_tab':    _badge_read_tab,
    'browser_screenshot':  _badge_screenshot,
    ...
}

badge_fn = _BROWSER_BADGE_DISPATCH.get(fn_name)
if badge_fn is not None:
    badge_fn(meta, fn_name, display_text, chars, is_screenshot)
```

---

## Continuous Integration & Testing

### CI Pipeline

Every push to `main` and every pull request triggers the
[GitHub Actions CI workflow](../../actions/workflows/ci.yml):

| Job | What it checks | Python versions | Blocking? |
|---|---|---|---|
| **Lint** | `ruff check` on `lib/`, `routes/`, `tests/` | 3.12 | ✅ Yes |
| **Unit Tests** | `pytest -m unit` + coverage — pure logic, no server/browser/network | 3.10, 3.12 | ✅ Yes |
| **API Tests** | `pytest -m api` + coverage — Flask test client + mock LLM server | 3.12 | ✅ Yes |
| **Healthcheck** | `python healthcheck.py` — syntax, imports, schema, assets | 3.12 | ✅ Yes |
| **Visual E2E** | `pytest -m visual` — Playwright browser tests (main branch only) | 3.12 | ❌ Non-blocking |

Unit tests run on a **Python 3.10 + 3.12 matrix** to catch compatibility issues
early. Coverage reports are generated as XML artifacts for each run.

### Running Tests Locally

```bash
# Quick: just unit tests
make test-unit

# Full CI pipeline (lint + unit + api + healthcheck)
make ci

# With coverage report
make test-coverage

# Individual commands
make lint              # Ruff linter + format check
make lint-fix          # Auto-fix lint issues
make test-api          # API integration tests
make test-visual       # Playwright E2E tests (needs: playwright install chromium)
make test-all          # All tests including visual
make smoke             # Smoke tests only (imports, syntax, blueprints)
make healthcheck       # Project diagnostics
```

Or use pytest directly:

```bash
python -m pytest -m unit -v                    # Unit tests, verbose
python -m pytest -m api --tb=long              # API tests, full tracebacks
python -m pytest -k "test_build_body" -v       # Filter by name
python -m pytest tests/test_smoke.py -v        # Just smoke tests
python -m pytest -m unit --cov=lib --cov=routes --cov-report=term-missing  # With coverage
python tests/run_all.py --unit                 # Legacy test runner
```

### Test Organization

Tests live in two directories:

| Directory | Purpose | Run in CI? | Style |
|-----------|---------|------------|-------|
| `tests/` | Structured, self-contained pytest tests with mocks | ✅ Yes | pytest classes + `@pytest.mark.*` markers |
| `debug/` | Manual exploration scripts, benchmarks, API-dependent tests | ❌ No | Standalone `python debug/script.py` |

When a `debug/test_*.py` script proves valuable for regression prevention,
it should be migrated to `tests/` with proper mocking and markers. See
[`tests/README.md`](tests/README.md) for the full test directory layout.

### Test Markers

Every test **must** have a marker. `strict_markers = true` in `pyproject.toml`
means unknown or missing markers cause a hard error.

| Marker | Meaning | Needs server? | Needs browser? |
|---|---|---|---|
| `@pytest.mark.unit` | Pure logic tests | No | No |
| `@pytest.mark.api` | Flask test client + mock LLM | Test server | No |
| `@pytest.mark.visual` | Playwright E2E | Live server | Yes (Chromium) |
| `@pytest.mark.slow` | Takes > 10 seconds | Varies | Varies |

### Adding New Tests

1. Create test functions/classes in `tests/test_*.py`
2. Add the appropriate `@pytest.mark.*` decorator to every class or function
3. Verify locally: `make test-unit` (or the relevant marker)
4. Ensure `make ci` passes before submitting a PR

### Reporting Bugs

When filing a bug report on GitHub:

1. Visit `http://localhost:5001/api/support-bundle` to get a diagnostic JSON bundle
2. Open a [Bug Report issue](../../issues/new?template=bug_report.yml)
3. Paste the diagnostic bundle JSON into the designated field
4. Include relevant log excerpts from `logs/error.log` — use the request ID (`rid:XXXX`)
   from error messages to find related entries in `logs/app.log`

The diagnostic bundle includes system info, recent errors (sanitized), and active
configuration — **API keys are automatically redacted**.

---

## Bug Prevention Checklist

Before submitting code, verify:

### Logging & Error Handling
- [ ] Every new `.py` file has `from lib.log import get_logger; logger = get_logger(__name__)`
- [ ] All `logger.error()` in `except` blocks include `exc_info=True`
- [ ] No `logger.info()` in `# HOT_PATH` modules (use `debug` instead)
- [ ] No bare `except:` or `except Exception: pass` (always log or re-raise)
- [ ] No f-strings in log calls (use %-style: `logger.info('x=%s', x)`)
- [ ] Operations > 1 second use `log_context()` or `log_external()`

### Safety & Robustness
- [ ] All `json.loads()` calls on DB/user data are wrapped in try/except
- [ ] All HTTP calls (`requests.*`, `session.*`) include a `timeout=` parameter
- [ ] String variables used after `content.strip()` in except blocks are pre-defined

### Architecture
- [ ] Protocol interfaces are used for cross-module boundaries (not concrete imports)
- [ ] Package façades use `lib/_pkg_utils.py` helpers for optional sub-module imports
- [ ] New tool handlers are registered via `@tool_registry.handler()` (not if/elif chains)
- [ ] New packages follow the [façade pattern](#anatomy-of-a-façade) with `build_facade()`
- [ ] Any new per-request module has `# HOT_PATH` as the first line
- [ ] New protocols are added to `lib/protocols.py` with `@runtime_checkable` and `__all__`
