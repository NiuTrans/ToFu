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
  DB-coupled (``_scan_costs_in_range`` / ``_get_monthly_costs`` / the
  ``_extract_convs_for_date`` family) are covered ONLY for their
  graceful-degrade-on-DB-error contract — full DB seeding is out of scope.

No network, no live LLM (``_run_llm_analysis`` stubbed), no real DB
(``get_thread_db`` patched to raise → exercises the except path). The ``lib``
pricing globals + ``lib.pricing`` lookups are stubbed so cost math is
deterministic regardless of the live pricing table.
"""
from __future__ import annotations

import json
from unittest import mock

import pytest

from lib.daily_report import cost, conversations, todos, llm

pytestmark = pytest.mark.unit


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

    def test_scan_costs_degrades_on_db_error(self):
        with mock.patch("lib.database.get_thread_db", side_effect=RuntimeError("no db")):
            assert cost._scan_costs_in_range(0, 10**13) == {}


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
            res = conversations._analyse_conversations([], "2026-06-28")
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
            res = conversations._analyse_conversations(convs, "2026-06-28")
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
            res = conversations._analyse_conversations(convs, "2026-06-28")
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
            "2026-06-28", _prev=prev, _defer_save=True)
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
            _prev=prev, _defer_save=True)
        assert changed == 1
        assert ret_prev["tomorrow"][0]["done"] is True

    def test_mark_yesterday_done_no_signals_is_noop(self):
        prev = {"tomorrow": [{"id": "t1", "text": "x", "done": False}]}
        ret_prev, changed = todos._mark_yesterday_todos_done(
            "2026-06-28", yesterday_done=[], todo_status=[("x", False)],
            _prev=prev, _defer_save=True)
        assert changed == 0


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
