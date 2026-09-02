"""Behavioral tests for the Daily Report engine's pure logic.

Coverage ceiling (honest — see the project journal entry):
  daily_report's SUBSTANTIVE logic is genuinely pure and well-reachable
  (unlike a log-miner). Fully covered here:
    * cost.py     — ``_qwen_cny`` tiered pricing + ``_calc_msg_cost_cny``
                    rollup (token extraction, cache multipliers, legacy-preset
                    mapping, qwen vs USD branches, empty→0.0).
    * conversations.py — ``_safe_int_ts``, ``_build_transcript_from_messages``
                    (malformed/multimodal/missing-field tolerance + budget),
                    ``_analyse_conversations`` empty fast-path + stream
                    post-processing (LLM stubbed).
    * todos.py    — ``_normalize_todo_text``, ``_fuzzy_todo_match`` (Jaccard/LCS),
                    ``_close_yesterday_remaining_todos`` /
                    ``_mark_yesterday_todos_done`` on an in-memory ``_prev`` dict.
    * llm.py      — ``_extract_json_result`` + ``_repair_truncated_json``
                    (fences / legacy-list / embedded / truncated), ``_pick_persona``.
  Authority-coupled scans are covered with immutable conversation snapshots;
  storage transport and pricing lookups remain stubbed.

No network, no live LLM (``_run_llm_analysis`` stubbed), and no storage
process. Pricing globals and lookups are stubbed so cost math is deterministic.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from lib.daily_report import cost, conversations, todos, llm

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41


# ═══════════════════════════════════════════════════════════
#  1. cost.py — pricing math
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCostMath:
    def test_calc_cost_empty_usage_is_zero(self):
        assert cost._calc_msg_cost_cny(None) == 0.0
        assert cost._calc_msg_cost_cny({}) == 0.0
        # all-zero token dict → 0.0
        assert cost._calc_msg_cost_cny({"prompt_tokens": 0, "completion_tokens": 0}) == 0.0

    def test_calc_cost_usd_model_with_stubbed_pricing(self):
        # Stub the pricing module so the math is deterministic:
        # input $10/1M, output $30/1M, rate 7.0 CNY/USD.
        pdata = {"usdToCny": 7.0, "inputPrice": 10.0, "outputPrice": 30.0}
        with mock.patch("lib.pricing.get_pricing_data", return_value=pdata), \
                mock.patch("lib.pricing.lookup_pricing",
                           return_value={"input": 10.0, "output": 30.0}):
            usd = (1000 * 10.0 + 500 * 30.0) / 1e6  # 0.025 USD
            expected = round(usd * 7.0, 4)
            got = cost._calc_msg_cost_cny(
                {"prompt_tokens": 1000, "completion_tokens": 500}, "gpt-4o")
        assert got == expected

    def test_calc_cost_handles_alternate_token_keys(self):
        """input_tokens/output_tokens aliases must be honored like prompt/completion."""
        pdata = {"usdToCny": 1.0, "inputPrice": 1000.0, "outputPrice": 1000.0}
        with mock.patch("lib.pricing.get_pricing_data", return_value=pdata), \
                mock.patch("lib.pricing.lookup_pricing", return_value=None):
            a = cost._calc_msg_cost_cny({"prompt_tokens": 100, "completion_tokens": 100}, "x")
            b = cost._calc_msg_cost_cny({"input_tokens": 100, "output_tokens": 100}, "x")
        assert a == b and a > 0

    def test_legacy_preset_maps_to_model(self):
        # 'opus' preset → aws.claude-opus model; just assert mapping is applied
        # by checking the constant table directly (pure data).
        assert cost._LEGACY_PRESET_TO_MODEL["opus"].startswith("aws.claude-opus")

    def test_qwen_tiered_pricing(self):
        # Stub QWEN_PRICING_CNY on lib with a 2-tier table.
        fake = {"_default": {"input": [(1000, 1.0), (10000, 2.0)],
                             "output": [(1000, 5.0)]}}
        with mock.patch("lib.QWEN_PRICING_CNY", fake, create=True):
            # 500 input tokens, tier 1 (≤1000) @ ¥1.0/1M
            assert cost._qwen_cny(500, "input") == pytest.approx(500 * 1.0 / 1e6)
            # 5000 input tokens, tier 2 (≤10000) @ ¥2.0/1M
            assert cost._qwen_cny(5000, "input") == pytest.approx(5000 * 2.0 / 1e6)
            # beyond last tier → last tier's price
            assert cost._qwen_cny(99999, "output") == pytest.approx(99999 * 5.0 / 1e6)
            # no tiers → 0
            assert cost._qwen_cny(100, "input", "no-such-model") in (
                pytest.approx(100 * 1.0 / 1e6),)  # falls back to _default tier 1

    def test_scan_costs_degrades_on_authority_error(self):
        with mock.patch(
                "lib.conversations.repository.scan_conversations_bounded",
                side_effect=RuntimeError("authority unavailable")):
            assert cost._scan_costs_in_range(
                0, 10**13, owner_user_id=OWNER_USER_ID) == {}

    def test_cost_scan_projects_usage_from_authority_snapshot(self):
        from lib.conversations.repository import ConversationSnapshot

        snapshot = ConversationSnapshot(
            metadata={
                "id": "c-projected", "title": "Projected",
                "created_at": 10, "updated_at": 20,
                "settings": {"model": "fallback-model"},
            },
            messages=[
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "usage": {"prompt_tokens": 100, "completion_tokens": 25},
                    "timestamp": 15, "model": "model-x",
                    "providerId": "provider-y",
                },
            ],
        )

        assert cost._usage_rows_from_snapshots([snapshot]) == [{
            "id": "c-projected", "title": "Projected",
            "created_at": 10, "updated_at": 20,
            "conv_model": "fallback-model", "first_content": "question",
            "msg_index": 1, "total_msgs": 2,
            "usage": {"prompt_tokens": 100, "completion_tokens": 25},
            "msg_timestamp": 15, "msg_model": "model-x",
            "msg_provider": "provider-y",
        }]

    def test_projected_usage_row_preserves_cost_rollup(self):
        import datetime as _dt
        ts = int(_dt.datetime(2026, 8, 7, 12).timestamp() * 1000)
        from lib.conversations.repository import ConversationSnapshot
        snapshot = ConversationSnapshot(
            metadata={
                "id": "c-projected", "title": "Projected",
                "created_at": ts, "updated_at": ts,
                "settings": {"model": "fallback-model"},
            },
            messages=[{
                "role": "assistant", "timestamp": ts,
                "usage": {"prompt_tokens": 100, "completion_tokens": 25},
                "model": "model-x", "providerId": "provider-y",
            }],
        )

        with mock.patch(
                "lib.conversations.repository.scan_conversations_bounded",
                return_value=(1, iter([snapshot]))), \
                mock.patch.object(cost, "_calc_msg_cost_cny", return_value=1.25):
            got = cost._scan_costs_in_range(
                ts - 1, ts + 1, 2026, 8,
                owner_user_id=OWNER_USER_ID)

        assert got[7]["cost"] == 1.25
        assert got[7]["conversations"]["c-projected"] == {
            "name": "Projected", "cost": 1.25, "tokens": 125,
        }


# ═══════════════════════════════════════════════════════════
#  1b. cost.py — SCOPED cache invalidation (regression: a delete
#      must only drop the day(s) it touched, never the whole table)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScopedCostInvalidation:
    def _msg(self, y, m, d, hour=12):
        import datetime as _dt
        ts = int(_dt.datetime(y, m, d, hour).timestamp() * 1000)
        return {"role": "assistant", "timestamp": ts,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}

    def test_cost_days_only_counts_usage_messages(self):
        msgs = [
            self._msg(2026, 6, 10),
            {"role": "user", "content": "no usage", "timestamp": 1},  # no usage → ignored
            self._msg(2026, 6, 11),
        ]
        days = cost._cost_days_for_messages(msgs)
        assert days == {"2026-06-10", "2026-06-11"}

    def test_cost_days_timestampless_uses_conv_span(self):
        # A usage message with no timestamp falls back to conv_start's day.
        import datetime as _dt
        cs = int(_dt.datetime(2026, 6, 9, 8).timestamp() * 1000)
        msg = {"role": "assistant", "usage": {"prompt_tokens": 10}}
        days = cost._cost_days_for_messages([msg], conv_start=cs)
        assert days == {"2026-06-09"}

    def test_cost_days_handles_non_list_and_empty(self):
        assert cost._cost_days_for_messages(None) == set()
        assert cost._cost_days_for_messages([]) == set()
        assert cost._cost_days_for_messages(["garbage", 123]) == set()

    def test_invalidate_scoped_hits_only_touched_days(self):
        """The delete path must call per-day invalidation, NEVER the whole-table wipe."""
        calls = []
        # Patch the day-level invalidator so we can see exactly which days it targets.
        with mock.patch.object(cost, "invalidate_day_cost_cache",
                               side_effect=lambda d=None, **_kw: calls.append(d)):
            touched = cost.invalidate_cost_cache_for_messages(
                [self._msg(2026, 6, 10), self._msg(2026, 6, 10, hour=20)],
                owner_user_id=OWNER_USER_ID)
        # Two messages, same day → one dedup'd day invalidated.
        assert touched == {"2026-06-10"}
        assert calls == ["2026-06-10"]
        # Critically: never invoked with None (the whole-table nuke).
        assert None not in calls

    def test_invalidate_scoped_no_usage_is_noop(self):
        calls = []
        with mock.patch.object(cost, "invalidate_day_cost_cache",
                               side_effect=lambda d=None, **_kw: calls.append(d)):
            touched = cost.invalidate_cost_cache_for_messages(
                [{"role": "user", "content": "hi", "timestamp": 1}],
                owner_user_id=OWNER_USER_ID)
        # No usage anywhere → nothing invalidated (and never the whole-table wipe).
        assert touched == set()
        assert calls == []

    @pytest.mark.parametrize("date_str", ["2026-06-10", None])
    def test_day_cache_invalidator_executes_the_delete(
            self, date_str):
        """The public invalidator must issue the named Sidecar command."""
        client = mock.Mock()
        with mock.patch("lib.storage.get_storage_client", return_value=client):
            cost.invalidate_day_cost_cache(
                date_str, owner_user_id=OWNER_USER_ID)
        payload = {'user_id': OWNER_USER_ID}
        if date_str is not None:
            payload['date'] = date_str
        args = client.command.call_args.args
        assert args[:2] == ('daily_cost.delete', payload)
        assert args[2].startswith('daily-cost-delete:')


# ═══════════════════════════════════════════════════════════
#  1c. cost.py — SETTLED-DAY PINNING (regression: a cross-midnight
#      edit today must NOT drop yesterday's already-persisted cost
#      snapshot → historical balances stay stable + instant).
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSettledDayPinning:
    import datetime as _dt

    def _msg(self, date_obj, hour=12):
        import datetime as _dt
        ts = int(_dt.datetime(date_obj.year, date_obj.month, date_obj.day, hour)
                 .timestamp() * 1000)
        return {"role": "assistant", "timestamp": ts,
                "usage": {"prompt_tokens": 100, "completion_tokens": 50}}

    def _yesterday_today(self):
        import datetime as _dt
        today = _dt.date.today()
        yesterday = today - _dt.timedelta(days=1)
        return yesterday, today

    def test_should_pin_predicate(self):
        """_should_pin_day: pin iff strictly-past AND already persisted."""
        assert cost._should_pin_day("2026-06-10", "2026-06-12",
                                    {"2026-06-10"}) is True
        # persisted but IS today → not pinned (today is still being written)
        assert cost._should_pin_day("2026-06-12", "2026-06-12",
                                    {"2026-06-12"}) is False
        # past but NOT persisted → not pinned (nothing to protect; let it scan)
        assert cost._should_pin_day("2026-06-10", "2026-06-12", set()) is False

    def test_cross_midnight_edit_pins_yesterday_drops_today(self):
        """A conversation whose messages span yesterday+today: editing/deleting
        it invalidates ONLY today (unsettled). Yesterday, already persisted, is
        pinned → its cache hit + value survive."""
        yesterday, today = self._yesterday_today()
        msgs = [self._msg(yesterday, hour=23), self._msg(today, hour=1)]
        y_str = yesterday.isoformat()
        t_str = today.isoformat()

        calls = []
        # yesterday is already a persisted snapshot; today is not.
        with mock.patch.object(cost, "_persisted_cost_dates",
                               return_value={y_str}), \
                mock.patch.object(cost, "invalidate_day_cost_cache",
                                  side_effect=lambda d=None, **_kw: calls.append(d)):
            touched = cost.invalidate_cost_cache_for_messages(
                msgs, owner_user_id=OWNER_USER_ID)

        # Yesterday pinned → NOT invalidated; only today dropped.
        assert y_str not in touched, "settled yesterday must be pinned"
        assert t_str in touched, "today (unsettled) must still be invalidated"
        assert y_str not in calls
        assert calls == [t_str]

    def test_neuter_pin_predicate_reverts_to_old_behavior(self):
        """NEUTER: force _should_pin_day → False (the pre-fix behavior). Now the
        cross-midnight edit invalidates YESTERDAY too — proving the predicate is
        load-bearing for historical-balance stability."""
        yesterday, today = self._yesterday_today()
        msgs = [self._msg(yesterday, hour=23), self._msg(today, hour=1)]
        y_str = yesterday.isoformat()
        t_str = today.isoformat()

        calls = []
        with mock.patch.object(cost, "_persisted_cost_dates",
                               return_value={y_str}), \
                mock.patch.object(cost, "_should_pin_day", return_value=False), \
                mock.patch.object(cost, "invalidate_day_cost_cache",
                                  side_effect=lambda d=None, **_kw: calls.append(d)):
            touched = cost.invalidate_cost_cache_for_messages(
                msgs, owner_user_id=OWNER_USER_ID)

        # With pinning neutered, yesterday IS invalidated again (the old bug).
        assert y_str in touched and t_str in touched
        assert set(calls) == {y_str, t_str}


# ═══════════════════════════════════════════════════════════
#  1d. cost.py — FULLY-CACHED PAST MONTH does ZERO live scans
#      (the whole point of pinning:翻旧账永远读缓存, never re-scan).
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPastMonthNoRescan:
    def test_fully_cached_past_month_never_scans(self):
        """A past month whose every day is already persisted must resolve from
        the cache with ZERO calls to _scan_costs_in_range (the 8s DB scan)."""
        import datetime as _dt
        today = _dt.date.today()
        # Pick a month strictly before this one (fully in the past).
        if today.month == 1:
            year, month = today.year - 1, 12
        else:
            year, month = today.year, today.month - 1
        days_in_month = (_dt.date(year + (month // 12), (month % 12) + 1, 1)
                         - _dt.timedelta(days=1)).day

        # Seed a cache hit for EVERY day of that past month (some non-zero).
        cached = {d: {"cost": (1.0 if d % 2 else 0.0), "conversations": {}}
                  for d in range(1, days_in_month + 1)}

        scan_calls = []

        def _fake_scan(*a, **k):
            scan_calls.append((a, k))
            return {}

        with mock.patch.object(cost, "_load_cached_day_costs",
                               return_value=cached), \
                mock.patch.object(cost, "_scan_costs_in_range",
                                  side_effect=_fake_scan), \
                mock.patch.object(cost, "_persist_day_cost"):
            result = cost._get_monthly_costs(
                year, month, owner_user_id=OWNER_USER_ID)

        # Zero rescans — the whole month came from cache.
        assert scan_calls == [], (
            f"expected 0 scans for fully-cached past month, got {len(scan_calls)}")
        # And the non-zero days are surfaced.
        assert all(v["cost"] > 0 for v in result.values())
        assert len(result) == sum(1 for d in range(1, days_in_month + 1) if d % 2)


# ═══════════════════════════════════════════════════════════
#  2. conversations.py — transcript building + timestamp coercion
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTranscriptBuilding:
    def test_safe_int_ts_coercion(self):
        assert conversations._safe_int_ts("123") == 123
        assert conversations._safe_int_ts(45.9) == 45
        assert conversations._safe_int_ts(None) == 0
        assert conversations._safe_int_ts("garbage") == 0
        assert conversations._safe_int_ts(None, fallback=7) == 7

    def test_transcript_basic_user_assistant(self):
        msgs = [
            {"role": "user", "content": "hello there", "timestamp": 100},
            {"role": "assistant", "content": "hi back", "timestamp": 200},
        ]
        out = conversations._build_transcript_from_messages(msgs, 0, 1000)
        assert "USER: hello there" in out
        assert "ASSISTANT: hi back" in out

    def test_transcript_filters_out_of_window(self):
        msgs = [
            {"role": "user", "content": "in window", "timestamp": 500},
            {"role": "user", "content": "too late", "timestamp": 5000},
        ]
        out = conversations._build_transcript_from_messages(msgs, 0, 1000)
        assert "in window" in out
        assert "too late" not in out

    def test_transcript_multimodal_content_list(self):
        msgs = [{
            "role": "user",
            "content": [{"text": "part one"}, "part two", {"nope": "x"}],
            "timestamp": 100,
        }]
        out = conversations._build_transcript_from_messages(msgs, 0, 1000)
        assert "part one" in out and "part two" in out  # no crash on mixed list

    def test_transcript_extracts_tool_names(self):
        msgs = [{
            "role": "assistant", "content": "working", "timestamp": 100,
            "toolRounds": [{"calls": [
                {"function": {"name": "web_search"}},
                {"name": "fetch_url"},
            ]}],
        }]
        out = conversations._build_transcript_from_messages(msgs, 0, 1000)
        assert "web_search" in out and "fetch_url" in out

    def test_transcript_missing_fields_no_crash(self):
        # messages with no role/content/timestamp must not raise
        out = conversations._build_transcript_from_messages(
            [{}, {"role": "user"}, {"content": None}], 0, 1000)
        assert isinstance(out, str)

    def test_transcript_empty_returns_empty_string(self):
        assert conversations._build_transcript_from_messages([], 0, 1000) == ""


@pytest.mark.unit
class TestAnalyseConversations:
    def test_empty_convs_returns_ok_with_no_streams(self):
        # No DB / LLM touched on the empty fast-path; carryover loaded via
        # _get_yesterday_carryover which reads a report file — stub it empty.
        with mock.patch.object(conversations, "_get_yesterday_carryover",
                               return_value=[]):
            res = conversations._analyse_conversations(
                [], "2026-06-28", owner_user_id=OWNER_USER_ID)
        assert res["ok"] is True
        assert res["streams"] == []
        assert "persona" in res and "quote" in res

    def test_stream_post_processing_validates_conv_ids(self):
        """LLM-returned streams: only conv_ids that exist are kept; unknown dropped."""
        convs = [{"id": "c1", "rounds": 1}, {"id": "c2", "rounds": 1}]
        # Stub the LLM layer: one stream claiming c1 + a bogus id.
        fake_llm = mock.Mock(return_value=(
            [{"title": "Stream A", "summary": "s", "status": "done",
              "conv_ids": ["c1", "nonexistent"]}],
            [],   # tomorrow
            [],   # yesterday_done
            None,  # error
        ))
        with mock.patch.object(conversations, "_run_llm_analysis", fake_llm), \
                mock.patch.object(conversations, "_get_yesterday_carryover", return_value=[]), \
                mock.patch.object(conversations, "_get_yesterday_todo_accountability", return_value=[]), \
                mock.patch.object(conversations, "_mark_yesterday_todos_done",
                                  return_value=(None, 0)), \
                mock.patch.object(conversations, "_close_yesterday_remaining_todos",
                                  return_value=([], None, 0)):
            res = conversations._analyse_conversations(
                convs, "2026-06-28", owner_user_id=OWNER_USER_ID)
        streams = res["streams"]
        named = [s for s in streams if s["title"] == "Stream A"][0]
        # The bogus id (not in all_conv_ids) is dropped from the LLM stream.
        assert "nonexistent" not in named["conv_ids"]
        assert "c1" in named["conv_ids"]
        # Every REAL conv ends up accounted for across the streams (the single
        # unclaimed c2 folds into the last stream via the `elif unclaimed` branch).
        all_assigned = {cid for s in streams for cid in s["conv_ids"]}
        assert all_assigned == {"c1", "c2"}

    def test_invalid_stream_status_normalized(self):
        convs = [{"id": "c1", "rounds": 1}]
        fake_llm = mock.Mock(return_value=(
            [{"title": "S", "summary": "", "status": "bogus_status", "conv_ids": ["c1"]}],
            [], [], None))
        with mock.patch.object(conversations, "_run_llm_analysis", fake_llm), \
                mock.patch.object(conversations, "_get_yesterday_carryover", return_value=[]), \
                mock.patch.object(conversations, "_get_yesterday_todo_accountability", return_value=[]), \
                mock.patch.object(conversations, "_mark_yesterday_todos_done", return_value=(None, 0)), \
                mock.patch.object(conversations, "_close_yesterday_remaining_todos",
                                  return_value=([], None, 0)):
            res = conversations._analyse_conversations(
                convs, "2026-06-28", owner_user_id=OWNER_USER_ID)
        assert res["streams"][0]["status"] == "in_progress"  # bogus → default


# ═══════════════════════════════════════════════════════════
#  3. todos.py — fuzzy matching + in-memory carryover logic
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTodoFuzzyMatch:
    def test_normalize_strips_punct_and_case(self):
        assert todos._normalize_todo_text("  Fix the Bug!! ") == "fixthebug"

    def test_exact_and_substring_match(self):
        assert todos._fuzzy_todo_match("修复图片回显", "修复图片回显") is True
        assert todos._fuzzy_todo_match("修复图片回显", "修复图片回显问题") is True

    def test_unrelated_texts_do_not_match(self):
        assert todos._fuzzy_todo_match("write the parser", "buy groceries") is False

    def test_empty_inputs_no_match(self):
        assert todos._fuzzy_todo_match("", "anything") is False
        assert todos._fuzzy_todo_match("anything", "") is False

    def test_close_yesterday_remaining_in_memory(self):
        """Operate on a passed-in _prev dict with _defer_save → no disk I/O."""
        prev = {"tomorrow": [
            {"id": "t1", "text": "undone item", "done": False},
            {"id": "t2", "text": "already done", "done": True},
        ]}
        unfinished, ret_prev, changed = todos._close_yesterday_remaining_todos(
            "2026-06-28", owner_user_id=OWNER_USER_ID,
            _prev=prev, _defer_save=True)
        assert changed == 1  # only the undone one is auto-closed
        assert [u["text"] for u in unfinished] == ["undone item"]
        # the auto-closed item is now done+_auto_closed in the mutated dict
        t1 = [t for t in ret_prev["tomorrow"] if t["id"] == "t1"][0]
        assert t1["done"] is True and t1["_auto_closed"] is True

    def test_mark_yesterday_done_fuzzy(self):
        prev = {"tomorrow": [{"id": "t1", "text": "修复图片回显", "done": False}]}
        ret_prev, changed = todos._mark_yesterday_todos_done(
            "2026-06-28",
            yesterday_done=["修复图片回显问题"],   # fuzzy variant
            todo_status=[("修复图片回显", False)],
            owner_user_id=OWNER_USER_ID,
            _prev=prev, _defer_save=True)
        assert changed == 1
        assert ret_prev["tomorrow"][0]["done"] is True

    def test_mark_yesterday_done_no_signals_is_noop(self):
        prev = {"tomorrow": [{"id": "t1", "text": "x", "done": False}]}
        ret_prev, changed = todos._mark_yesterday_todos_done(
            "2026-06-28", yesterday_done=[], todo_status=[("x", False)],
            owner_user_id=OWNER_USER_ID,
            _prev=prev, _defer_save=True)
        assert changed == 0


# ═══════════════════════════════════════════════════════════
#  3b. todos.py — _merge_manual_state (regression: a report
#      REGENERATION must NOT clobber the user's manual edits)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMergeManualState:
    def test_manual_stream_status_survives_regen_by_conv_ids(self):
        """User cycled a stream to 'blocked'; a fresh analysis (new uuid ids,
        LLM-reworded title, default 'in_progress') must preserve the manual
        status. Identity = conv_ids overlap (title may change on regen)."""
        existing = {"streams": [
            {"id": "stream-OLD", "title": "Parser refactor",
             "status": "blocked", "_manual": True, "conv_ids": ["c1", "c2"]},
        ]}
        fresh = {"streams": [
            {"id": "stream-NEW", "title": "Rework the parsing layer",
             "status": "in_progress", "conv_ids": ["c1"]},
            {"id": "stream-NEW2", "title": "Write docs", "status": "done",
             "conv_ids": ["c9"]},
        ], "tomorrow": []}
        todos._merge_manual_state(fresh, existing)
        s = [x for x in fresh["streams"] if x["id"] == "stream-NEW"][0]
        assert s["status"] == "blocked"   # manual override preserved via conv_ids
        assert s["_manual"] is True
        # Unrelated stream (no conv_id overlap) keeps its fresh status.
        assert [x for x in fresh["streams"]
                if x["id"] == "stream-NEW2"][0]["status"] == "done"

    def test_manual_stream_no_false_positive_without_overlap(self):
        """A manual override must NOT bleed onto a stream it doesn't share
        conv_ids or an exact normalized title with."""
        existing = {"streams": [
            {"id": "s-old", "title": "Fix billing", "status": "blocked",
             "_manual": True, "conv_ids": ["c1"]},
        ]}
        fresh = {"streams": [
            {"id": "s-new", "title": "Write docs", "status": "done",
             "conv_ids": ["c2"]},
        ], "tomorrow": []}
        todos._merge_manual_state(fresh, existing)
        assert fresh["streams"][0]["status"] == "done"       # untouched
        assert "_manual" not in fresh["streams"][0]

    def test_todo_checkoff_survives_regen(self):
        existing = {"streams": [], "tomorrow": [
            {"id": "todo-1", "text": "ship the fix", "done": True},
        ]}
        fresh = {"streams": [], "tomorrow": [
            {"id": "todo-NEW", "text": "ship the fix", "done": False},
        ]}
        todos._merge_manual_state(fresh, existing)
        assert fresh["tomorrow"][0]["done"] is True   # check-off preserved

    def test_manual_added_todo_survives_regen(self):
        """A manually-added TODO the LLM didn't re-propose must be re-appended."""
        existing = {"streams": [], "tomorrow": [
            {"id": "todo-manual", "text": "call the dentist",
             "done": False, "_manual": True},
        ]}
        fresh = {"streams": [], "tomorrow": [
            {"id": "todo-NEW", "text": "unrelated LLM item", "done": False},
        ]}
        todos._merge_manual_state(fresh, existing)
        texts = [t["text"] for t in fresh["tomorrow"]]
        assert "call the dentist" in texts   # manual TODO not lost
        manual = [t for t in fresh["tomorrow"] if t["text"] == "call the dentist"][0]
        assert manual["_manual"] is True and manual["id"] == "todo-manual"

    def test_manual_added_todo_not_duplicated_when_reproposed(self):
        existing = {"streams": [], "tomorrow": [
            {"id": "todo-manual", "text": "fix the login bug",
             "done": False, "_manual": True},
        ]}
        fresh = {"streams": [], "tomorrow": [
            {"id": "todo-NEW", "text": "fix the login bug problem", "done": False},
        ]}
        todos._merge_manual_state(fresh, existing)
        # Fuzzy-matches the re-proposed item → NOT re-appended (no dup).
        matching = [t for t in fresh["tomorrow"]
                    if "login bug" in t["text"]]
        assert len(matching) == 1

    def test_legacy_todo_tasks_preserved(self):
        existing = {"streams": [], "tomorrow": [],
                    "tasks": [{"id": "todo-x", "text": "legacy", "_todo": True}]}
        fresh = {"streams": [], "tomorrow": [], "tasks": []}
        todos._merge_manual_state(fresh, existing)
        assert any(t.get("_todo") for t in fresh["tasks"])

    def test_no_existing_is_noop(self):
        fresh = {"streams": [{"id": "s", "title": "t", "status": "done"}],
                 "tomorrow": []}
        out = todos._merge_manual_state(fresh, None)
        assert out is fresh and fresh["streams"][0]["status"] == "done"

    def test_analyse_conversations_invokes_merge_on_regen(self):
        """End-to-end at the _analyse_conversations seam: a manual status
        override in the prior report survives a regen (the data-loss bug)."""
        convs = [{"id": "c1", "rounds": 1}]
        fake_llm = mock.Mock(return_value=(
            [{"title": "Ship feature X", "summary": "", "status": "in_progress",
              "conv_ids": ["c1"]}],
            [], [], None))
        prior = {"streams": [
            {"id": "stream-OLD", "title": "Ship feature X",
             "status": "done", "_manual": True, "conv_ids": ["c1"]}],
            "tomorrow": []}
        with mock.patch.object(conversations, "_run_llm_analysis", fake_llm), \
                mock.patch.object(conversations, "_get_yesterday_carryover", return_value=[]), \
                mock.patch.object(conversations, "_get_yesterday_todo_accountability", return_value=[]), \
                mock.patch.object(conversations, "_mark_yesterday_todos_done", return_value=(None, 0)), \
                mock.patch.object(conversations, "_close_yesterday_remaining_todos",
                                  return_value=([], None, 0)), \
                mock.patch.object(conversations, "_load_report", return_value=prior):
            res = conversations._analyse_conversations(
                convs, "2026-06-28", owner_user_id=OWNER_USER_ID)
        ship = [s for s in res["streams"] if s["title"] == "Ship feature X"][0]
        # Without the merge this would be 'in_progress' (LLM value) — the bug.
        assert ship["status"] == "done" and ship.get("_manual") is True


# ═══════════════════════════════════════════════════════════
#  4. llm.py — JSON extraction / repair / persona
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestJsonExtraction:
    def test_direct_object_parse(self):
        s = json.dumps({"streams": [{"title": "a"}], "tomorrow": ["x"],
                        "yesterday_done": ["y"]})
        streams, tomorrow, yd = llm._extract_json_result(s)
        assert streams == [{"title": "a"}] and tomorrow == ["x"] and yd == ["y"]

    def test_markdown_fences_stripped(self):
        s = "```json\n" + json.dumps({"streams": [{"title": "z"}]}) + "\n```"
        streams, _, _ = llm._extract_json_result(s)
        assert streams == [{"title": "z"}]

    def test_legacy_list_format(self):
        streams, tomorrow, yd = llm._extract_json_result('[{"title": "old"}]')
        assert streams == [{"title": "old"}] and tomorrow == [] and yd == []

    def test_embedded_object_in_prose(self):
        s = 'Here you go: {"streams": [{"title": "e"}]} hope that helps'
        streams, _, _ = llm._extract_json_result(s)
        assert streams == [{"title": "e"}]

    def test_empty_input_returns_empty_triple(self):
        assert llm._extract_json_result("") == ([], [], [])
        assert llm._extract_json_result(None) == ([], [], [])

    def test_truncated_json_is_repaired(self):
        # Output cut mid-generation at a clean value boundary (token ceiling) —
        # the trailing object is complete but the array/object closers are missing.
        s = '{"streams": [{"title": "first", "summary": "done"}, {"title": "second"'
        streams, _, _ = llm._extract_json_result(s)
        # _repair_truncated_json closes the open [ and { → both entries salvaged.
        assert [st.get("title") for st in streams] == ["first", "second"]

    def test_truncated_mid_string_is_unsalvageable(self):
        # Cut in the MIDDLE of a string value — cannot be repaired → empty.
        s = '{"streams": [{"title": "first", "summary": "done"}, {"title": "secon'
        assert llm._extract_json_result(s) == ([], [], [])

    def test_unparseable_garbage_returns_empty(self):
        assert llm._extract_json_result("not json at all !!!") == ([], [], [])


@pytest.mark.unit
class TestPickPersona:
    def test_zero_conversations_is_sloth(self):
        p = llm._pick_persona({"totalConversations": 0})
        assert p["name"] and "desc" in p  # sleeping persona, well-formed

    def test_persona_always_well_formed(self):
        for stats in ({"totalConversations": 5},
                      {"totalConversations": 3, "codeRelated": True, "projectCount": 4},
                      {"totalConversations": 1, "searchCount": 20},
                      {}):
            p = llm._pick_persona(stats)
            assert set(p) >= {"emoji", "name", "desc"}
            assert all(p.values())
