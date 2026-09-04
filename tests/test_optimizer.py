"""Behavioral tests for the Daily Optimizer's decision logic.

Coverage ceiling (honest — see the project journal entry):
  * ``lib/optimizer/analyzer.py`` (734 LOC) is mostly log-file + DB readers
    (``_collect_*`` walk ``APP_LOG``/``AUDIT_LOG_FILE``/``ERROR_LOG`` and the
    SYSTEM/CHAT DBs). DB-backed collectors are unit-tested at their repository
    and graceful-degrade boundaries, not against a real database. The pure scoring /
    aggregation slice IS exercised against a real temp log file:
    ``_collect_app_log_signals`` (regex counting) and ``_collect_recurring_issues``
    (fingerprint clustering with ``min_count``), plus the pure parsers and
    ``_domain_of``.
  * The HIGH-VALUE decision surfaces are fully covered with stubs:
      - ``actions.ACTION_REGISTRY`` well-formedness + unknown-action handling
      - ``proposer._validate_proposal`` (non-whitelist coercion, malformed
        rejection, severity/confidence/ttl clamping)
      - ``applier.apply_proposal`` whitelist GATE (auto-apply vs pending_review
        vs unknown vs dry_run vs apply-failure→rejected) — asserting the gate
        DECISION, never a live config mutation.

No network, no live LLM (proposer takes an ``llm_override`` seam), no real DB
(``storage`` is patched on the ``applier`` namespace). Deterministic, ``unit``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest import mock

import pytest

from lib.identity import PrincipalContext
from lib.optimizer import applier, orchestrator, proposer
from lib.optimizer import analyzer
from lib.optimizer.actions import ACTION_REGISTRY, get_action

pytestmark = pytest.mark.unit
_OWNER_USER_ID = 23


def _optimizer_principal(owner_user_id: int = _OWNER_USER_ID):
    return PrincipalContext.system(
        subject_id=f'optimizer-test-{owner_user_id}',
        owner_user_id=owner_user_id,
        scopes={'optimizer:maintain'},
    )


# ═══════════════════════════════════════════════════════════
#  1. Action registry — well-formedness + lookup
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestActionRegistry:
    def test_every_action_is_well_formed(self):
        for name, entry in ACTION_REGISTRY.items():
            assert entry["name"] == name, f"{name}: name mismatch"
            assert "auto_apply" in entry and isinstance(entry["auto_apply"], bool)
            assert entry.get("description"), f"{name}: missing description"
            # auto-apply actions MUST carry a callable apply handler;
            # suggest-only actions MUST NOT (they land in pending_review).
            if entry["auto_apply"]:
                assert callable(entry["apply"]), f"{name}: auto_apply w/o apply()"
                assert callable(entry["revert"]), f"{name}: auto_apply w/o revert()"
            else:
                assert entry["apply"] is None, f"{name}: suggest-only has apply()"
            assert entry.get('deployment_modes'), (
                f'{name}: missing deployment availability contract')

    def test_only_block_search_domain_auto_applies(self):
        auto = [n for n, e in ACTION_REGISTRY.items() if e["auto_apply"]]
        assert auto == ["block_search_domain"], (
            f"v1 whitelist must auto-apply ONLY block_search_domain, got {auto}"
        )

    def test_get_action_unknown_returns_none(self):
        assert get_action("no_such_action") is None

    def test_proposer_allowed_types_subset_of_registry(self):
        """Every action_type the LLM may return must exist in the registry."""
        for t in proposer._ALLOWED_ACTION_TYPES:
            assert t in ACTION_REGISTRY, f"{t} advertised to LLM but not registered"


# ═══════════════════════════════════════════════════════════
#  2. proposer._validate_proposal — the trust boundary on LLM output
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestValidateProposal:
    def _base(self, **over):
        d = {
            "title": "Block spammy.example",
            "rationale": "12 IRRELEVANT drops in 24h",
            "action_type": "block_search_domain",
            "action_args": {"domain": "spammy.example"},
            "severity": "low",
            "confidence": 0.8,
            "ttl_days": 7,
            "evidence_ids": ["e1"],
        }
        d.update(over)
        return d

    def test_valid_proposal_passes(self):
        out = proposer._validate_proposal(self._base())
        assert out is not None
        assert out["action_type"] == "block_search_domain"
        assert out["action_args"]["domain"] == "spammy.example"
        # ttl back-filled into args
        assert out["action_args"]["ttl_days"] == 7

    def test_non_dict_rejected(self):
        assert proposer._validate_proposal("not a dict") is None  # type: ignore[arg-type]
        assert proposer._validate_proposal(None) is None  # type: ignore[arg-type]

    def test_missing_required_fields_rejected(self):
        assert proposer._validate_proposal(self._base(title="")) is None
        assert proposer._validate_proposal(self._base(rationale="")) is None
        assert proposer._validate_proposal(self._base(action_type="")) is None

    def test_non_whitelist_action_coerced_to_other(self):
        out = proposer._validate_proposal(self._base(action_type="rm_rf_root"))
        assert out is not None
        assert out["action_type"] == "other"  # coerced, NOT passed through

    def test_bad_severity_defaults_low(self):
        out = proposer._validate_proposal(self._base(severity="catastrophic"))
        assert out["severity"] == "low"

    def test_confidence_clamped(self):
        assert proposer._validate_proposal(self._base(confidence=9.9))["confidence"] == 1.0
        assert proposer._validate_proposal(self._base(confidence=-5))["confidence"] == 0.0
        # non-numeric confidence → default 0.5, not a crash
        assert proposer._validate_proposal(self._base(confidence="high"))["confidence"] == 0.5

    def test_ttl_clamped_to_1_30(self):
        assert proposer._validate_proposal(self._base(ttl_days=999))["ttl_days"] == 30
        assert proposer._validate_proposal(self._base(ttl_days=0))["ttl_days"] == 7  # 0→falsy→default 7
        assert proposer._validate_proposal(self._base(ttl_days=-3))["ttl_days"] == 1

    def test_malformed_action_args_becomes_empty_dict(self):
        out = proposer._validate_proposal(self._base(action_args="not-a-dict"))
        assert out["action_args"] == {"ttl_days": 7}  # reset to {} then ttl back-filled


# ═══════════════════════════════════════════════════════════
#  3. proposer.propose — JSON parsing + LLM-failure resilience
#     (llm_override seam: no live LLM)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProposeWithStubbedLLM:
    def _evidence(self):
        return analyzer.EvidenceBundle(window_hours=24, generated_at="2026-06-28T00:00:00")

    def test_well_formed_response_yields_proposals(self):
        payload = {"proposals": [{
            "title": "Block junk.example",
            "rationale": "noise",
            "action_type": "block_search_domain",
            "action_args": {"domain": "junk.example"},
            "severity": "low", "confidence": 0.7, "ttl_days": 5,
        }]}
        out = proposer.propose(self._evidence(),
                               llm_override=lambda msgs: (json.dumps(payload), {}))
        assert len(out) == 1 and out[0]["action_args"]["domain"] == "junk.example"

    def test_response_with_json_fences_is_stripped(self):
        body = "```json\n" + json.dumps({"proposals": []}) + "\n```"
        out = proposer.propose(self._evidence(), llm_override=lambda msgs: (body, {}))
        assert out == []

    def test_invalid_json_returns_empty_not_crash(self):
        out = proposer.propose(self._evidence(),
                               llm_override=lambda msgs: ("{not json", {}))
        assert out == []

    def test_missing_proposals_key_returns_empty(self):
        out = proposer.propose(self._evidence(),
                               llm_override=lambda msgs: (json.dumps({"foo": 1}), {}))
        assert out == []

    def test_llm_raising_returns_empty(self):
        def boom(msgs):
            raise RuntimeError("llm down")
        out = proposer.propose(self._evidence(), llm_override=boom)
        assert out == []

    def test_malformed_item_dropped_valid_kept(self):
        payload = {"proposals": [
            {"title": "", "rationale": "x", "action_type": "other"},   # malformed
            {"title": "Keep", "rationale": "y", "action_type": "other"},  # valid
        ]}
        out = proposer.propose(self._evidence(),
                               llm_override=lambda msgs: (json.dumps(payload), {}))
        assert len(out) == 1 and out[0]["title"] == "Keep"


# ═══════════════════════════════════════════════════════════
#  4. applier.apply_proposal — the whitelist AUTO-APPLY GATE
#     storage + action apply() stubbed; we assert the gate DECISION.
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestApplyProposalGate:
    @pytest.fixture
    def _stub_storage(self):
        """Patch storage on the applier namespace; create_proposal returns a
        deterministic id and the other writes are no-ops we can assert on."""
        with mock.patch.object(applier, "storage") as st:
            st.create_proposal.return_value = "opt_test123"
            yield st

    def test_whitelisted_action_auto_applies(self, _stub_storage):
        prop = {"title": "Block x", "rationale": "r",
                "action_type": "block_search_domain",
                "action_args": {"domain": "x.example"}, "ttl_days": 7}
        # stub the action handler so no real config file is touched
        fake_apply = mock.Mock(return_value={"domain": "x.example"})
        with mock.patch.dict(applier.ACTION_REGISTRY,
                             {"block_search_domain": {"name": "block_search_domain",
                                                      "auto_apply": True,
                                                      "apply": fake_apply,
                                                      "revert": lambda a, **kw: None,
                                                      "deployment_modes": frozenset({'personal'})}}):
            res = applier.apply_proposal(
                prop, owner_user_id=_OWNER_USER_ID)
        assert res["status"] == "applied"
        fake_apply.assert_called_once()
        _stub_storage.record_applied.assert_called_once()  # learning-loop log written
        assert _stub_storage.create_proposal.call_args.kwargs[
            'owner_user_id'] == _OWNER_USER_ID
        assert _stub_storage.record_applied.call_args.kwargs[
            'owner_user_id'] == _OWNER_USER_ID

    def test_suggest_only_action_does_NOT_auto_apply(self, _stub_storage):
        prop = {"title": "Tune timeout", "rationale": "r",
                "action_type": "adjust_fetch_timeout", "action_args": {}}
        res = applier.apply_proposal(
            prop, owner_user_id=_OWNER_USER_ID)
        assert res["status"] == "pending_review"
        assert res["detail"] == "not in auto-apply whitelist"
        _stub_storage.record_applied.assert_not_called()  # nothing applied

    def test_unknown_action_lands_in_pending_review(self, _stub_storage):
        prop = {"title": "Mystery", "rationale": "r",
                "action_type": "totally_unknown", "action_args": {}}
        res = applier.apply_proposal(
            prop, owner_user_id=_OWNER_USER_ID)
        assert res["status"] == "pending_review"
        assert "unknown action_type" in res["detail"]
        _stub_storage.record_applied.assert_not_called()

    def test_dry_run_never_applies_even_whitelisted(self, _stub_storage):
        prop = {"title": "Block x", "rationale": "r",
                "action_type": "block_search_domain",
                "action_args": {"domain": "x.example"}}
        fake_apply = mock.Mock(return_value={})
        with mock.patch.dict(applier.ACTION_REGISTRY,
                             {"block_search_domain": {"name": "block_search_domain",
                                                      "auto_apply": True,
                                                      "apply": fake_apply,
                                                      "revert": lambda a, **kw: None,
                                                      "deployment_modes": frozenset({'personal'})}}):
            res = applier.apply_proposal(
                prop, owner_user_id=_OWNER_USER_ID, dry_run=True)
        assert res["status"] == "pending_review"
        assert res["detail"] == "dry_run"
        fake_apply.assert_not_called()

    def test_apply_handler_failure_marks_rejected(self, _stub_storage):
        prop = {"title": "Block x", "rationale": "r",
                "action_type": "block_search_domain",
                "action_args": {"domain": "x.example"}}
        boom = mock.Mock(side_effect=OSError("disk full"))
        with mock.patch.dict(applier.ACTION_REGISTRY,
                             {"block_search_domain": {"name": "block_search_domain",
                                                      "auto_apply": True,
                                                      "apply": boom,
                                                      "revert": lambda a, **kw: None,
                                                      "deployment_modes": frozenset({'personal'})}}):
            res = applier.apply_proposal(
                prop, owner_user_id=_OWNER_USER_ID)
        assert res["status"] == "rejected"
        assert "disk full" in res["error"]
        # rejected proposals are NOT recorded as applied
        _stub_storage.record_applied.assert_not_called()
        _stub_storage.update_proposal_status.assert_called_once()
        assert _stub_storage.update_proposal_status.call_args.kwargs[
            'owner_user_id'] == _OWNER_USER_ID

    def test_distributed_mode_never_mutates_personal_config(
            self, _stub_storage, monkeypatch):
        monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
        prop = {
            'title': 'Block x', 'rationale': 'r',
            'action_type': 'block_search_domain',
            'action_args': {'domain': 'x.example'},
        }
        fake_apply = mock.Mock(return_value={})
        with mock.patch.dict(applier.ACTION_REGISTRY, {
                'block_search_domain': {
                    'name': 'block_search_domain',
                    'auto_apply': True,
                    'apply': fake_apply,
                    'revert': mock.Mock(),
                    'deployment_modes': frozenset({'personal'}),
                }}):
            result = applier.apply_proposal(
                prop, owner_user_id=_OWNER_USER_ID)

        assert result['status'] == 'pending_review'
        assert result['detail'] == 'action unavailable in this deployment mode'
        fake_apply.assert_not_called()
        _stub_storage.record_applied.assert_not_called()


# ═══════════════════════════════════════════════════════════
#  5. block_search_domain — pure normalization + idempotent apply/revert
#     (config file redirected to a temp path — no real server_config.json)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBlockSearchDomainAction:
    def test_normalise_domain_strips_scheme_www_path_port(self):
        from lib.optimizer.actions import block_search_domain as b
        assert b._normalise_domain("https://www.Spammy.Example:8080/path") == "spammy.example"
        assert b._normalise_domain("  HTTP://Foo.com  ") == "foo.com"
        assert b._normalise_domain("") == ""

    def test_apply_rejects_invalid_domain(self, tmp_path, monkeypatch):
        from lib.optimizer.actions import block_search_domain as b
        # point config at a temp file and stub reload_config so apply() is pure
        monkeypatch.setattr(b, "_CONFIG_FILE", str(tmp_path / "server_config.json"))
        monkeypatch.setattr(b._lib, "reload_config", lambda: None)
        with pytest.raises(ValueError):
            b.apply(
                {"domain": "not-a-domain"},
                owner_user_id=_OWNER_USER_ID)  # no dot → invalid

    def test_apply_then_revert_roundtrip(self, tmp_path, monkeypatch):
        from lib.optimizer.actions import block_search_domain as b
        cfg = str(tmp_path / "server_config.json")
        monkeypatch.setattr(b, "_CONFIG_FILE", cfg)
        monkeypatch.setattr(b._lib, "reload_config", lambda: None)
        monkeypatch.setattr(b._lib, "SKIP_DOMAINS", set(), raising=False)

        out = b.apply(
            {"domain": "junk.example", "ttl_days": 3},
            owner_user_id=_OWNER_USER_ID)
        assert out["domain"] == "junk.example"
        data = json.loads(open(cfg).read())
        assert "junk.example" in data["search"]["skip_domains"]

        rev = b.revert(
            {"domain": "junk.example"}, owner_user_id=_OWNER_USER_ID)
        assert rev["reverted"] is True
        data2 = json.loads(open(cfg).read())
        assert "junk.example" not in data2["search"]["skip_domains"]

    def test_revert_missing_domain_is_noop(self, tmp_path, monkeypatch):
        from lib.optimizer.actions import block_search_domain as b
        monkeypatch.setattr(b, "_CONFIG_FILE", str(tmp_path / "server_config.json"))
        monkeypatch.setattr(b._lib, "reload_config", lambda: None)
        rev = b.revert(
            {"domain": "never.added"}, owner_user_id=_OWNER_USER_ID)
        assert rev["reverted"] is False and rev["reason"] == "not_present"


# ═══════════════════════════════════════════════════════════
#  6. analyzer pure-logic slice — parsers + the scoring/clustering
#     functions, exercised against a REAL temp log file.
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAnalyzerPureLogic:
    def test_parse_app_log_ts(self):
        ts = analyzer._parse_app_log_ts("2026-06-28 01:02:03 [INFO] hello")
        assert ts == datetime(2026, 6, 28, 1, 2, 3)
        assert analyzer._parse_app_log_ts("no timestamp here") is None

    def test_parse_audit_line_tolerates_garbage(self):
        assert analyzer._parse_audit_line('{"event":"x"}') == {"event": "x"}
        assert analyzer._parse_audit_line("not json") is None

    def test_audit_ts_aware_handles_z_suffix_and_bad(self):
        assert analyzer._audit_ts_aware({"timestamp": "2026-06-28T00:00:00Z"}) is not None
        assert analyzer._audit_ts_aware({"timestamp": "garbage"}) is None
        assert analyzer._audit_ts_aware({}) is None

    def test_domain_of_normalization(self):
        assert analyzer._domain_of("https://www.Example.com:443/x") == "example.com"
        assert analyzer._domain_of("ftp://nope") == ""
        assert analyzer._domain_of("") == ""

    def test_conversation_tool_scan_uses_bounded_transcript_batches(
            self, monkeypatch):
        from lib.conversations import repository

        cutoff = datetime(2026, 8, 29, 12, 30)
        captured = {}

        def scan(**kwargs):
            captured.update(kwargs)
            return 1, iter([{'messages': [{'toolRounds': [
                {
                    'toolName': 'web_search',
                    'results': [
                        {'url': 'https://www.Example.com/a'},
                        {'url': 'https://example.com/b'},
                    ],
                },
                {
                    'toolName': 'fetch_url',
                    'args': {'url': 'https://Docs.Example.org/page'},
                },
            ]}]}])

        monkeypatch.setattr(repository, 'scan_conversations_bounded', scan)

        result = analyzer._collect_conversation_tool_distribution(
            cutoff, owner_user_id=_OWNER_USER_ID)

        assert captured == {
            'user_id': _OWNER_USER_ID,
            'updated_at_gte': int(cutoff.timestamp() * 1000),
            'limit': 200,
            'settings_keys': [],
        }
        assert result == {
            'tool_counts': {'web_search': 1, 'fetch_url': 1},
            'search_urls': [{'domain': 'example.com', 'count': 2}],
            'fetch_urls': [{'domain': 'docs.example.org', 'count': 1}],
        }

    def test_conversation_tool_scan_discards_partial_counts_on_lazy_error(
            self, monkeypatch):
        from lib.conversations import repository

        def rows():
            yield {'messages': [{'toolRounds': [
                {'toolName': 'web_search', 'results': []},
            ]}]}
            raise RuntimeError('later transcript frame failed')

        monkeypatch.setattr(
            repository,
            'scan_conversations_bounded',
            lambda **_kwargs: (2, rows()),
        )

        result = analyzer._collect_conversation_tool_distribution(
            datetime(2026, 8, 29), owner_user_id=_OWNER_USER_ID)

        assert result == {
            'tool_counts': {}, 'search_urls': [], 'fetch_urls': [],
        }

    def test_classify_error_signature(self):
        assert analyzer._classify_error_signature("xx KeyError yy") == "KeyError"
        assert analyzer._classify_error_signature("PREMATURE STREAM CLOSE now") == "PREMATURE STREAM CLOSE"
        assert analyzer._classify_error_signature("a normal info line") == ""

    def test_safe_tail_lines_missing_file_returns_empty(self, tmp_path):
        assert analyzer._safe_tail_lines(str(tmp_path / "nope.log")) == []

    def test_collect_app_log_signals_counts_real_lines(self, tmp_path, monkeypatch):
        """The scoring core: well-formed log in → expected aggregated counts out."""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        log = tmp_path / "app.log"
        log.write_text("\n".join([
            f"{ts} [INFO] [Tool:web_search] called",
            f"{ts} [INFO] [Tool:web_search] called",
            f"{ts} [INFO] [Tool:fetch_url] failed",
            f"{ts} [WARNING] [Fetch] Request failed for x",
            f"{ts} [INFO] [Fetch] Timeout after 30s",
            f"{ts} [INFO] got a 429 rate-limit",
            f"{ts} [INFO] [Search] IRRELEVANT dropped example.com",
            f"{ts} [ERROR] PromptTooLong",
            f"{ts} [WARNING] context near full",
            f"{ts} [INFO] [Compaction] compacting history",
            f"{ts} WARNING [LLM] candidate excerpt",
        ]) + "\n")
        monkeypatch.setattr(analyzer, "APP_LOG", str(log))
        out = analyzer._collect_app_log_signals(now - timedelta(hours=1))
        assert out["tool_call_counts"]["web_search"] == 2
        assert out["tool_error_counts"]["fetch_url"] == 1
        assert out["fetch_failure_count"] == 1
        assert out["fetch_timeout_count"] == 1
        assert out["rate_limit_429_count"] == 1
        assert out['irrelevant_dropped_domains'] == [
            {'domain': 'example.com', 'count': 1},
        ]
        assert out['prompt_too_long_count'] == 1
        assert out['context_near_full_count'] == 1
        assert out['compaction_trigger_count'] == 1
        assert out['warn_excerpts'] == [
            f'{ts} WARNING [LLM] candidate excerpt',
        ]

    def test_collect_app_log_signals_skips_non_candidate_timestamp_parse(
            self, monkeypatch):
        from lib.optimizer.analyzer import _logs

        now = datetime.now()
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        parse_timestamp = mock.Mock(wraps=_logs._parse_app_log_ts)
        monkeypatch.setattr(_logs, '_parse_app_log_ts', parse_timestamp)

        out = analyzer._collect_app_log_signals(
            now - timedelta(hours=1),
            log_lines=(
                f'{timestamp} [INFO] ordinary request completed',
                'not even a timestamp and no optimizer marker',
                f'{timestamp} [INFO] [Tool:web_search] called',
            ),
        )

        assert out['tool_call_counts'] == {'web_search': 1}
        assert parse_timestamp.call_count == 1

    def test_collect_app_log_signals_preserves_long_mixed_case_marker(self):
        from lib.optimizer.analyzer import _logs

        now = datetime.now()
        timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        padding = 'x' * (_logs._APP_LOG_FAST_MARKER_CHARACTER_LIMIT + 1)

        out = analyzer._collect_app_log_signals(
            now - timedelta(hours=1),
            log_lines=(
                f'{timestamp} {padding} [sEaRcH] IRRELEVANT dropped long.example',
            ),
        )

        assert out['irrelevant_dropped_domains'] == [
            {'domain': 'long.example', 'count': 1},
        ]

    def test_collect_app_log_signals_excludes_old_lines(self, tmp_path, monkeypatch):
        now = datetime.now()
        old_ts = (now - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        log = tmp_path / "app.log"
        log.write_text(f"{old_ts} [INFO] [Tool:web_search] called\n")
        monkeypatch.setattr(analyzer, "APP_LOG", str(log))
        out = analyzer._collect_app_log_signals(now - timedelta(hours=1))
        assert out["tool_call_counts"] == {}  # the 48h-old line is filtered out

    def test_collect_recurring_issues_clusters_by_signature(self, tmp_path, monkeypatch):
        """Clustering core: only signatures with count>=min_count are returned."""
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        err = tmp_path / "error.log"
        err.write_text("\n".join([
            f"{ts} [ERROR] KeyError: 'a'",
            f"{ts} [ERROR] KeyError: 'b'",       # 2nd KeyError → recurring
            f"{ts} [ERROR] ConnectionError once",  # only 1 → below min_count
        ]) + "\n")
        # audit source empty
        empty_audit = tmp_path / "audit.log"
        empty_audit.write_text("")
        monkeypatch.setattr(analyzer, "ERROR_LOG", str(err))
        monkeypatch.setattr(analyzer, "AUDIT_LOG_FILE", str(empty_audit))
        out = analyzer._collect_recurring_issues(
            now - timedelta(hours=1),
            now - timedelta(hours=1),
            min_count=2,
            owner_user_id=_OWNER_USER_ID,
            allow_unowned=True,
        )
        labels = {c["fingerprint"] for c in out}
        assert "errorlog::KeyError" in labels
        assert "errorlog::ConnectionError" not in labels  # below threshold

    def test_combined_audit_scan_preserves_all_projections(self):
        from lib.optimizer.analyzer import _audit

        now = datetime.now().astimezone()
        old = now - timedelta(days=2)
        entries = (
            {
                'timestamp': now.isoformat(),
                'event': 'optimizer_reviewed',
                'user_id': _OWNER_USER_ID,
                'detail': 'kept',
            },
            {
                'timestamp': now.isoformat(),
                'event': 'model_switch',
                'user_id': _OWNER_USER_ID,
                'old': 'model-a',
                'new': 'model-b',
                'reason': 'fallback',
            },
            *(
                {
                    'timestamp': now.isoformat(),
                    'event': 'tool_error',
                    'user_id': _OWNER_USER_ID,
                    'fingerprint': 'same-failure',
                    'detail': f'failure-{index}',
                    'tool': 'fetch_url',
                    'exc_type': 'TimeoutError',
                }
                for index in range(2)
            ),
            {
                'timestamp': now.isoformat(),
                'event': 'optimizer_foreign',
                'user_id': _OWNER_USER_ID + 1,
            },
            {
                'timestamp': old.isoformat(),
                'event': 'optimizer_old',
                'user_id': _OWNER_USER_ID,
            },
        )
        lines = tuple(json.dumps(entry) for entry in entries)

        evidence = _audit._collect_audit_log_evidence(
            now - timedelta(hours=1),
            owner_user_id=_OWNER_USER_ID,
            allow_unowned=False,
            log_lines=lines,
        )
        issues = analyzer._collect_recurring_issues(
            now - timedelta(hours=1),
            now - timedelta(hours=1),
            owner_user_id=_OWNER_USER_ID,
            allow_unowned=False,
            audit_issue_clusters=evidence['tool_error_clusters'],
            error_issue_clusters={},
        )

        assert evidence['audit_event_counts'] == {
            'optimizer_reviewed': 1,
            'model_switch': 1,
            'tool_error': 2,
        }
        assert evidence['model_switch_events'] == [{
            'timestamp': now.isoformat(),
            'old': 'model-a',
            'new': 'model-b',
            'reason': 'fallback',
            'error': '',
        }]
        assert len(evidence['optimizer_events']) == 1
        assert [(row['fingerprint'], row['count']) for row in issues] == [
            ('tool_error::same-failure', 2),
        ]

    def test_combined_error_scan_preserves_timestamp_split_semantics(self):
        from lib.optimizer.analyzer import _issues

        now = datetime.now()
        recent = now.strftime('%Y-%m-%d %H:%M:%S')
        old = (now - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        recent_first = f'{recent} [ERROR] KeyError first'
        recent_second = f'{recent} [ERROR] KeyError second'
        lines = (
            f'{recent} [INFO] ordinary line',
            '[ERROR] KeyError without timestamp',
            recent_first,
            f'{old} [ERROR] KeyError old',
            recent_second,
        )

        excerpts, clusters = _issues._collect_error_log_evidence(
            now - timedelta(hours=1),
            log_lines=lines,
            max_excerpt_lines=2,
        )
        recurring = analyzer._collect_recurring_issues(
            now - timedelta(hours=1),
            now - timedelta(hours=1),
            owner_user_id=_OWNER_USER_ID,
            allow_unowned=True,
            audit_issue_clusters={},
            error_issue_clusters=clusters,
        )

        assert excerpts == [recent_first, recent_second]
        assert [(row['fingerprint'], row['count']) for row in recurring] == [
            ('errorlog::KeyError', 3),
        ]

    def test_post_apply_metrics_scan_shared_app_snapshot_once(
            self, monkeypatch):
        from lib.optimizer.analyzer import _metrics

        now = datetime.now()
        recent = now.strftime('%Y-%m-%d %H:%M:%S')
        old = (now - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
        lines = (
            f'{recent} [INFO] [Search] IRRELEVANT dropped a.example',
            f'{recent} [INFO] [Search] IRRELEVANT dropped b.example',
            f'{recent} [ERROR] [Tool:fetch_url] failed',
            f'{old} [INFO] [Search] IRRELEVANT dropped a.example',
        )
        actions = [
            {
                'id': index,
                'p_action_type': 'block_search_domain',
                'p_action_args': json.dumps({'domain': domain}),
                'outcome_metric': '',
            }
            for index, domain in enumerate(
                ('a.example', 'b.example', 'a.example'), start=1)
        ]
        monkeypatch.setattr(
            analyzer.storage, 'list_applied_actions',
            mock.Mock(return_value=actions))
        record_metric = mock.Mock()
        monkeypatch.setattr(
            analyzer.storage, 'record_outcome_metric', record_metric)
        parse_timestamp = mock.Mock(wraps=_metrics._parse_app_log_ts)
        monkeypatch.setattr(_metrics, '_parse_app_log_ts', parse_timestamp)
        monkeypatch.setattr(
            _metrics, '_safe_tail_lines',
            mock.Mock(side_effect=AssertionError('snapshot must be reused')))

        result = analyzer._compute_post_apply_metrics(
            now - timedelta(hours=1),
            owner_user_id=_OWNER_USER_ID,
            allow_unowned_observability=True,
            app_log_lines=lines,
        )

        assert parse_timestamp.call_count == len(lines)
        assert [row['outcome_metric']['irrelevant_dropped_24h']
                for row in result] == [1, 1, 1]
        assert [row['outcome_metric']['total_tool_errors_24h']
                for row in result] == [1, 1, 1]
        assert record_metric.call_count == 3

    def test_gather_evidence_reads_each_personal_log_tail_once(
            self, tmp_path, monkeypatch):
        from lib.optimizer.analyzer import _audit, _issues, _model

        monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'personal')
        now = datetime.now().astimezone()
        local_timestamp = now.strftime('%Y-%m-%d %H:%M:%S')
        paths = {
            'APP_LOG': tmp_path / 'app.log',
            'ERROR_LOG': tmp_path / 'error.log',
            'AUDIT_LOG_FILE': tmp_path / 'audit.log',
        }
        paths['APP_LOG'].write_text(
            f'{local_timestamp} [ERROR] [Tool:fetch_url] failed\n')
        paths['ERROR_LOG'].write_text(
            f'{local_timestamp} [ERROR] KeyError first\n'
            f'{local_timestamp} [ERROR] KeyError second\n')
        paths['AUDIT_LOG_FILE'].write_text(json.dumps({
            'timestamp': now.isoformat(),
            'event': 'optimizer_snapshot_test',
            'user_id': _OWNER_USER_ID,
        }) + '\n')
        for constant, path in paths.items():
            monkeypatch.setattr(analyzer, constant, str(path))

        original_tail = _model._safe_tail_lines
        tail_paths: list[str] = []

        def counted_tail(path, *args, **kwargs):
            tail_paths.append(path)
            return original_tail(path, *args, **kwargs)

        monkeypatch.setattr(_model, '_safe_tail_lines', counted_tail)
        parse_audit_line = mock.Mock(wraps=_audit._parse_audit_line)
        parse_error_timestamp = mock.Mock(wraps=_issues._parse_app_log_ts)
        monkeypatch.setattr(_audit, '_parse_audit_line', parse_audit_line)
        monkeypatch.setattr(
            _issues, '_parse_app_log_ts', parse_error_timestamp)
        monkeypatch.setattr(
            analyzer.storage, 'list_applied_actions', mock.Mock(return_value=[]))
        monkeypatch.setattr(
            _model, '_collect_scheduler_signals',
            lambda **_kwargs: {
                'failing_scheduled_tasks': [], 'idle_proactive_tasks': []})
        monkeypatch.setattr(
            _model, '_collect_cost_outliers',
            lambda **_kwargs: {'top_cost_conversations': []})
        monkeypatch.setattr(
            _model, '_collect_conversation_tool_distribution',
            lambda *_args, **_kwargs: {
                'tool_counts': {}, 'search_urls': [], 'fetch_urls': []})
        monkeypatch.setattr(
            _model, '_collect_daily_report_snippets', lambda **_kwargs: [])

        bundle = analyzer.gather_evidence(
            principal=_optimizer_principal(), window_hours=24)

        assert tail_paths.count(str(paths['APP_LOG'])) == 1
        assert tail_paths.count(str(paths['ERROR_LOG'])) == 1
        assert tail_paths.count(str(paths['AUDIT_LOG_FILE'])) == 1
        assert bundle.tool_error_counts == {'fetch_url': 1}
        assert bundle.audit_event_counts == {'optimizer_snapshot_test': 1}
        assert len(bundle.recurring_issues) == 1
        assert parse_audit_line.call_count == 1
        assert parse_error_timestamp.call_count == 2

    def test_gather_evidence_degrades_when_dbs_unavailable(self, tmp_path, monkeypatch):
        """The DB-backed collectors must degrade gracefully (no raise) — point
        all log constants at empty temp files and let the DB calls fail."""
        for const in ("APP_LOG", "ERROR_LOG", "AUDIT_LOG_FILE"):
            p = tmp_path / f"{const}.log"
            p.write_text("")
            monkeypatch.setattr(analyzer, const, str(p))
        # Make the prior-actions DB read fail → exercises the except path
        monkeypatch.setattr(analyzer.storage, "list_applied_actions",
                            mock.Mock(side_effect=RuntimeError("no db")))
        bundle = analyzer.gather_evidence(
            principal=_optimizer_principal(), window_hours=24)
        assert isinstance(bundle, analyzer.EvidenceBundle)
        assert bundle.prior_actions == []  # degraded cleanly


def test_orchestrator_propagates_one_principal_and_owner(monkeypatch):
    principal = _optimizer_principal()
    evidence = analyzer.EvidenceBundle(window_hours=12)
    proposal = {
        'title': 'Review', 'rationale': 'owner propagation',
        'action_type': 'other', 'action_args': {},
    }
    revert = mock.Mock(return_value=[])
    gather = mock.Mock(return_value=evidence)
    propose = mock.Mock(return_value=[proposal])
    apply = mock.Mock(return_value={
        'proposal_id': 'owner-proposal',
        'action_type': 'other',
        'status': 'pending_review',
    })
    monkeypatch.setattr(orchestrator.applier, 'revert_expired_actions', revert)
    monkeypatch.setattr(orchestrator.analyzer, 'gather_evidence', gather)
    monkeypatch.setattr(orchestrator.proposer, 'propose', propose)
    monkeypatch.setattr(orchestrator.applier, 'apply_proposal', apply)

    summary = orchestrator.run_once(
        principal=principal, dry_run=True, window_hours=12)

    revert.assert_called_once_with(owner_user_id=_OWNER_USER_ID)
    gather.assert_called_once_with(principal=principal, window_hours=12)
    apply.assert_called_once_with(
        proposal, owner_user_id=_OWNER_USER_ID, dry_run=True)
    assert len(summary['pending_review']) == 1


def test_orchestrator_rejects_missing_scope_or_owner_before_storage(monkeypatch):
    revert = mock.Mock()
    monkeypatch.setattr(orchestrator.applier, 'revert_expired_actions', revert)
    invalid = (
        (None, TypeError),
        (
            PrincipalContext.system(
                subject_id='optimizer-no-scope', owner_user_id=23,
                scopes=set()),
            PermissionError,
        ),
        (
            PrincipalContext.system(
                subject_id='optimizer-no-owner',
                scopes={'optimizer:maintain'}),
            PermissionError,
        ),
    )
    for principal, error in invalid:
        with pytest.raises(error):
            orchestrator.run_once(principal=principal)
    revert.assert_not_called()


def test_distributed_evidence_excludes_unowned_and_foreign_owner_logs(
        tmp_path, monkeypatch):
    from lib.optimizer.analyzer import _model

    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    now = datetime.now().astimezone()
    local_ts = now.strftime('%Y-%m-%d %H:%M:%S')
    audit_ts = now.isoformat()
    app_log = tmp_path / 'app.log'
    error_log = tmp_path / 'error.log'
    audit_log = tmp_path / 'audit.log'
    app_log.write_text(
        f'{local_ts} [WARNING] [Tool:web_search] foreign raw secret\n')
    error_log.write_text(
        f'{local_ts} [ERROR] foreign owner private failure\n')
    audit_log.write_text('\n'.join((
        json.dumps({
            'timestamp': audit_ts, 'event': 'optimizer_owner_event',
            'user_id': 7, 'detail': 'owner-seven-visible'}),
        json.dumps({
            'timestamp': audit_ts, 'event': 'optimizer_foreign_event',
            'user_id': 8, 'detail': 'owner-eight-secret'}),
        json.dumps({
            'timestamp': audit_ts, 'event': 'optimizer_unowned_event',
            'detail': 'unowned-secret'}),
    )) + '\n')
    monkeypatch.setattr(analyzer, 'APP_LOG', str(app_log))
    monkeypatch.setattr(analyzer, 'ERROR_LOG', str(error_log))
    monkeypatch.setattr(analyzer, 'AUDIT_LOG_FILE', str(audit_log))
    original_tail = _model._safe_tail_lines
    tail_paths: list[str] = []

    def counted_tail(path, *args, **kwargs):
        tail_paths.append(path)
        return original_tail(path, *args, **kwargs)

    monkeypatch.setattr(_model, '_safe_tail_lines', counted_tail)
    monkeypatch.setattr(
        _model, '_collect_scheduler_signals',
        lambda **_kwargs: {
            'failing_scheduled_tasks': [], 'idle_proactive_tasks': []})
    monkeypatch.setattr(
        _model, '_collect_cost_outliers',
        lambda **_kwargs: {'top_cost_conversations': []})
    monkeypatch.setattr(
        _model, '_collect_conversation_tool_distribution',
        lambda *_args, **_kwargs: {
            'tool_counts': {}, 'search_urls': [], 'fetch_urls': []})
    monkeypatch.setattr(
        _model, '_collect_daily_report_snippets', lambda **_kwargs: [])
    monkeypatch.setattr(
        _model, '_compute_post_apply_metrics', lambda *_args, **_kwargs: [])

    bundle = analyzer.gather_evidence(
        principal=_optimizer_principal(owner_user_id=7), window_hours=24)
    serialized = json.dumps(bundle.as_dict(), ensure_ascii=False)

    assert bundle.audit_event_counts == {'optimizer_owner_event': 1}
    assert bundle.tool_call_counts == {}
    assert bundle.error_log_excerpts == []
    assert 'owner-seven-visible' in serialized
    assert 'owner-eight-secret' not in serialized
    assert 'unowned-secret' not in serialized
    assert 'foreign raw secret' not in serialized
    assert 'foreign owner private failure' not in serialized
    assert tail_paths == [str(audit_log)]
