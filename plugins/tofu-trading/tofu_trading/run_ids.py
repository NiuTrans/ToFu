"""tofu_trading/run_ids.py — the single source of truth for minting run identifiers.

WHY THIS MODULE EXISTS
----------------------
Five call sites independently built run ids as
``f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"``. That format has SECOND
resolution, and three of those ids land in columns declared
``TEXT NOT NULL UNIQUE``:

    trading_sim_sessions.session_id    <- llm_simulator.run_simulation
    trading_autopilot_cycles.cycle_id  <- trading_autopilot/cycle.py
                                          web/handlers/trading_tasks.py

Measured: two calls in the same second return byte-identical ids
(``sim_20260729_170815`` twice), and the second INSERT raises
``sqlite3.IntegrityError: UNIQUE constraint failed``. In ``run_simulation`` that
exception was not caught, so it propagated out of the whole function — the run
aborted with no session row and nothing the user could read.

The trigger surface is wider than it looks:

  1. Double-clicking "start simulation" in the UI.
  2. Any programmatic or batch launch — a parameter sweep, or the planned
     quant-vs-LLM comparison, which by construction fires two runs back to back.
  3. Two users on a shared host: the old id carried no user id either, so the
     collision crossed tenants.
  4. Two runs inside one test — which is why "run the simulator twice" was
     literally inexpressible in the suite, and a structural reason end-to-end
     coverage stayed missing for so long.

WHY A MODULE RATHER THAN FIVE FIXED F-STRINGS
---------------------------------------------
Five hand-rolled copies of an id format is what produced the bug: one of them
(``trading_tasks.py``) already appended ``uuid4().hex[:6]`` and was therefore
correct, while the other five drifted. Patching them one by one would leave the
same five-way drift in place for the next id that gets added. Per the project's
single-source rule, the format lives here once.

WHY NOT "RETRY UNTIL IT DOESN'T COLLIDE"
----------------------------------------
A retry loop only shrinks the race window; it does not remove it, and it turns a
deterministic bug into an intermittent one that reproduces on someone else's
machine. Enough entropy makes the collision impossible in the first place.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['mint_run_id', 'RUN_ID_ENTROPY_HEX']


#: Hex characters of uuid4 entropy appended to every id. Eight hex chars is
#: 32 bits (~4.3e9 values): with even a thousand runs minted in the same second
#: the collision probability is ~1e-4, and realistic bursts are single digits.
#: Kept a named constant so a guard can assert the entropy was not quietly
#: trimmed back to a length where collisions return.
RUN_ID_ENTROPY_HEX = 8


def mint_run_id(prefix: str, *, uid: int | None = None) -> str:
    """Mint a unique run identifier.

    Format: ``{prefix}_{YYYYmmdd_HHMMSS}_{uid}_{uuid4 hex}``, with the ``uid``
    segment omitted when no user is known (background workers).

    The timestamp is kept for human readability — these ids appear in the UI and
    in logs, and "which run was this" should be answerable at a glance. It is
    NOT what makes the id unique; the uuid4 suffix is. Including the user id
    additionally means two tenants acting in the same second cannot collide even
    before the random part is considered.

    Args:
        prefix: Short kind marker, e.g. ``'sim'`` / ``'autopilot'`` / ``'brain'``.
        uid:    Owning user id, when the caller knows it.

    Returns:
        A unique id string safe to insert into a UNIQUE column.
    """
    if not prefix:
        raise ValueError('mint_run_id requires a non-empty prefix')

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    entropy = uuid.uuid4().hex[:RUN_ID_ENTROPY_HEX]
    if uid is None:
        return f'{prefix}_{stamp}_{entropy}'
    return f'{prefix}_{stamp}_u{uid}_{entropy}'
