"""Server role titles are the exact sentinels the browser localizes.

The server names the conversations it provisions for a role with fixed,
language-neutral titles; the browser shows them through localized labels
(frontend/src/conversation/presentation/shell-localization.ts). A title
renamed on one side only would show the raw English role name again.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from lib.router.binding import ROUTER_TITLE
from lib.router.forwarding import PROJECT_ROUTER_TITLE
from lib.router.watchdog.binding import WATCHDOG_TITLE

pytestmark = pytest.mark.unit
SHELL_LOCALIZATION = (Path(__file__).resolve().parents[2]
                      / 'frontend/src/conversation/presentation/shell-localization.ts')


def _browser_role_titles() -> dict[str, str]:
    source = SHELL_LOCALIZATION.read_text(encoding='utf-8')
    table = re.search(r'const ROLE_TITLE_KEYS[^=]*= new Map\(\[(.*?)\]\);', source, re.S)
    assert table, 'ROLE_TITLE_KEYS map not found in shell-localization.ts'
    return dict(re.findall(r"\['([^']+)', '([^']+)'\]", table.group(1)))


def test_every_server_role_title_has_a_localized_browser_label():
    assert _browser_role_titles() == {
        ROUTER_TITLE: 'globalRouter.entry',
        WATCHDOG_TITLE: 'routerWatchdog.entry',
        PROJECT_ROUTER_TITLE: 'projectCoordination.coordinator',
    }

