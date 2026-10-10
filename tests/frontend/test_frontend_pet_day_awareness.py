"""Backend report projection and the single repository day-digest emit seam."""
import re
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section

REPO = Path(__file__).resolve().parent.parent.parent
REPORT_CACHE_OWNER = REPO / "frontend" / "src" / "features" / "myday" / "report-cache.ts"
BACKGROUND_OWNER = REPO / "frontend" / "src" / "features" / "background.ts"

pytestmark = pytest.mark.unit


def _myday_source():
    """Source text of the migrated myday.js presenter."""
    return runtime_section("myday.js")


def test_myday_derives_digest_from_report_shape():
    """Read backend report fields rather than re-deriving day policy in JS."""
    src = REPORT_CACHE_OWNER.read_text(encoding="utf-8")
    assert "myDayDigestFromReport" in src, "typed digest builder missing"
    m = re.search(
        r"export function myDayDigestFromReport\(report: MyDayReport\): MyDayDigest \{(.*?)\n\}",
        src,
        re.S,
    )
    assert m, "could not isolate myDayDigestFromReport"
    body = m.group(1)
    assert "report.streams" in body and "report.today_todos" in body, \
        "digest must read the backend report arrays, not a client re-derivation"
    assert "totalConversations" in body, "digest must read stats.totalConversations"
    assert "stream?.status === 'done'" in body and "stream?.status === 'blocked'" in body, \
        "stream done/blocked must come from the backend status field"


def test_myday_emit_routes_through_single_cache_choke():
    """Panel writes and background refreshes publish through one repository;
    the demand-loaded presenter owns neither digest policy nor boot probes."""
    cache = REPORT_CACHE_OWNER.read_text(encoding="utf-8")
    background = BACKGROUND_OWNER.read_text(encoding="utf-8")
    presenter = _myday_source()

    publish = re.search(
        r"const publishReport = \(date: string, report: MyDayReport\): void => \{(.*?)\n  \};",
        cache,
        re.S,
    )
    assert publish and "publishDigest?.(myDayDigestFromReport(report))" in publish.group(1)
    store = re.search(
        r"const storeReport = \(date: string, report: MyDayReport\): Promise<void> => \{(.*?)\n  \};",
        cache,
        re.S,
    )
    assert store and "publishReport(date, report);" in store.group(1)
    assert "repository.storeReport(dateStr, report)" in presenter
    assert "myDayDigestFromReport" not in presenter and "tofu:day" not in presenter
    assert "createMyDayReportRepository" in background
    assert "new CustomEvent('tofu:day', { detail: digest })" in background
    assert "if (myDayBackgroundController) return Promise.resolve();" in background
    assert "if (myDayBackgroundStart) return myDayBackgroundStart;" in background
    for source in (presenter, background):
        assert "fetch('/api" not in source and 'fetch("/api' not in source
