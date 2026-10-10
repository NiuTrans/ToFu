#!/usr/bin/env python3
"""Contract for the boot-scoped pending-resume hint and its HTTP routes.

``record_boot_interrupted_conversations`` runs once per boot from startup
recovery; the routes let the browser list the interrupted conversations and
dismiss (or prune) the hint after a batch resume. The store stays a hint —
turn status authority never moves here.

    python3 -m pytest tests/api_keys/test_pending_resume.py -q
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.runtime_state_store import reset_for_test  # noqa: E402
from lib.tasks_pkg.manager import (  # noqa: E402
    dismiss_pending_resume,
    read_pending_resume,
    record_boot_interrupted_conversations,
)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_store():
    reset_for_test()
    yield
    reset_for_test()


def _result(conversation_ids, reason="process_killed"):
    return {
        "recoveredAttemptCount": len(conversation_ids),
        "recoveredTaskCount": len(conversation_ids),
        "conversationIds": list(conversation_ids),
        "interruptedReason": reason,
    }


def test_record_read_roundtrip():
    record_boot_interrupted_conversations(_result(["c1", "c2"]))
    payload = read_pending_resume()
    assert payload is not None
    assert payload["conversationIds"] == ["c1", "c2"]
    assert payload["interruptedReason"] == "process_killed"
    assert payload["recordedAt"] > 0


def test_record_dedupes_and_drops_empty_ids():
    record_boot_interrupted_conversations(_result(["c1", "", None, "c1", "c2"]))
    assert read_pending_resume()["conversationIds"] == ["c1", "c2"]


def test_empty_recovery_clears_previous_record():
    record_boot_interrupted_conversations(_result(["c1"]))
    record_boot_interrupted_conversations(_result([]))
    assert read_pending_resume() is None


def test_failed_recovery_keeps_previous_record():
    record_boot_interrupted_conversations(_result(["c1"]))
    record_boot_interrupted_conversations(None)
    record_boot_interrupted_conversations("garbage")
    assert read_pending_resume()["conversationIds"] == ["c1"]


def test_dismiss_all_clears():
    record_boot_interrupted_conversations(_result(["c1", "c2"]))
    dismiss_pending_resume()
    assert read_pending_resume() is None


def test_dismiss_subset_prunes_and_keeps_reason():
    record_boot_interrupted_conversations(_result(["c1", "c2", "c3"]))
    dismiss_pending_resume(["c2", "missing"])
    payload = read_pending_resume()
    assert payload["conversationIds"] == ["c1", "c3"]
    assert payload["interruptedReason"] == "process_killed"
    dismiss_pending_resume(["c1", "c3"])
    assert read_pending_resume() is None


def test_dismiss_empty_iterable_is_noop():
    record_boot_interrupted_conversations(_result(["c1"]))
    dismiss_pending_resume([])
    assert read_pending_resume()["conversationIds"] == ["c1"]


def test_record_is_capped():
    ids = [f"c{i}" for i in range(150)]
    record_boot_interrupted_conversations(_result(ids))
    assert len(read_pending_resume()["conversationIds"]) == 100


# ── HTTP routes ────────────────────────────────────────────────────


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {
            **Quart.default_config,
            'PROVIDE_AUTOMATIC_OPTIONS': True,
        }
    return Quart(__name__)


async def _body(response_or_pair):
    if isinstance(response_or_pair, tuple):
        response, status = response_or_pair
    else:
        response, status = response_or_pair, response_or_pair.status_code
    raw = await response.get_data(as_text=True)
    return status, json.loads(raw)


class _CatalogPage:
    def __init__(self, items):
        self.items = items
        self.total_count = len(items)
        self.has_more = False


def _catalog_row(conv_id, title, updated_at):
    return {
        "id": conv_id,
        "title": title,
        "msg_count": 3,
        "created_at": 1,
        "updated_at": updated_at,
        "settings": {},
        "rev": 1,
    }


def test_get_route_lists_owned_conversations_in_pending_order(monkeypatch):
    async def run():
        import routes.conversations as route
        from quart import g
        from lib.api_keys import local_admin_context

        record_boot_interrupted_conversations(
            _result(["c-new", "c-gone", "c-old"]))
        monkeypatch.setattr(
            route, "list_conversation_metadata_page",
            lambda *, user_id, limit, settings_keys, **kwargs: _CatalogPage([
                _catalog_row("c-old", "Older", 10),
                _catalog_row("c-new", "Newer", 20),
            ]),
        )

        async def _no_document(conv_id, user_id, **kwargs):
            return None

        monkeypatch.setattr(route, "_get_document", _no_document)
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/pending-resume'):
            g.auth_ctx = local_admin_context()
            status, body = await _body(await route.list_pending_resume())
        return status, body

    status, body = __import__('asyncio').run(run())
    assert status == 200
    assert body["reason"] == "process_killed"
    # pending order preserved; the conversation missing from the catalog page
    # fell back to a (monkeypatched empty) per-id lookup and was dropped.
    assert [item["id"] for item in body["conversations"]] == ["c-new", "c-old"]
    assert [item["title"] for item in body["conversations"]] == [
        "Newer", "Older"]


def test_get_route_empty_when_no_record():
    async def run():
        import routes.conversations as route
        from quart import g
        from lib.api_keys import local_admin_context

        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/pending-resume'):
            g.auth_ctx = local_admin_context()
            status, body = await _body(await route.list_pending_resume())
        return status, body

    status, body = __import__('asyncio').run(run())
    assert status == 200
    assert body["conversations"] == []


def test_dismiss_route_prunes_listed_ids():
    async def run():
        import routes.conversations as route
        from quart import g
        from lib.api_keys import local_admin_context

        record_boot_interrupted_conversations(_result(["c1", "c2"]))
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/pending-resume/dismiss',
                method='POST',
                json={"conversationIds": ["c1"]}):
            g.auth_ctx = local_admin_context()
            status, body = await _body(
                await route.dismiss_pending_resume_route())
        return status, body

    status, body = __import__('asyncio').run(run())
    assert status == 200
    assert read_pending_resume()["conversationIds"] == ["c2"]


def test_dismiss_route_without_ids_clears_everything():
    async def run():
        import routes.conversations as route
        from quart import g
        from lib.api_keys import local_admin_context

        record_boot_interrupted_conversations(_result(["c1", "c2"]))
        app = _make_app()
        async with app.test_request_context(
                '/api/v1/conversations/pending-resume/dismiss',
                method='POST',
                json={}):
            g.auth_ctx = local_admin_context()
            status, body = await _body(
                await route.dismiss_pending_resume_route())
        return status, body

    status, _body_json = __import__('asyncio').run(run())
    assert status == 200
    assert read_pending_resume() is None
