"""Behavioral tests for the Daily Optimizer's decision logic.

Coverage ceiling (honest — see the project journal entry):
  * ``lib/optimizer/analyzer.py`` (734 LOC) is mostly log-file + DB readers
    (``_collect_*`` walk ``APP_LOG``/``AUDIT_LOG_FILE``/``ERROR_LOG`` and the
    SYSTEM/CHAT DBs). The DB-backed collectors are NOT unit-tested beyond
    their graceful-degrade-on-error contract. The genuinely pure scoring /
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

from lib.optimizer import applier, proposer
from lib.optimizer import analyzer
from lib.optimizer.actions import ACTION_REGISTRY, get_action

pytestmark = pytest.mark.unit


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
                                                      "revert": lambda a: None}}):
            res = applier.apply_proposal(prop)
        assert res["status"] == "applied"
        fake_apply.assert_called_once()
        _stub_storage.record_applied.assert_called_once()  # learning-loop log written

    def test_suggest_only_action_does_NOT_auto_apply(self, _stub_storage):
        prop = {"title": "Tune timeout", "rationale": "r",
                "action_type": "adjust_fetch_timeout", "action_args": {}}
        res = applier.apply_proposal(prop)
        assert res["status"] == "pending_review"
        assert res["detail"] == "not in auto-apply whitelist"
        _stub_storage.record_applied.assert_not_called()  # nothing applied

    def test_unknown_action_lands_in_pending_review(self, _stub_storage):
        prop = {"title": "Mystery", "rationale": "r",
                "action_type": "totally_unknown", "action_args": {}}
        res = applier.apply_proposal(prop)
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
                                                      "revert": lambda a: None}}):
            res = applier.apply_proposal(prop, dry_run=True)
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
                                                      "revert": lambda a: None}}):
            res = applier.apply_proposal(prop)
        assert res["status"] == "rejected"
        assert "disk full" in res["error"]
        # rejected proposals are NOT recorded as applied
        _stub_storage.record_applied.assert_not_called()
        _stub_storage.update_proposal_status.assert_called_once()


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
            b.apply({"domain": "not-a-domain"})  # no dot → invalid

    def test_apply_then_revert_roundtrip(self, tmp_path, monkeypatch):
        from lib.optimizer.actions import block_search_domain as b
        cfg = str(tmp_path / "server_config.json")
        monkeypatch.setattr(b, "_CONFIG_FILE", cfg)
        monkeypatch.setattr(b._lib, "reload_config", lambda: None)
        monkeypatch.setattr(b._lib, "SKIP_DOMAINS", set(), raising=False)

        out = b.apply({"domain": "junk.example", "ttl_days": 3})
        assert out["domain"] == "junk.example"
        data = json.loads(open(cfg).read())
        assert "junk.example" in data["search"]["skip_domains"]

        rev = b.revert({"domain": "junk.example"})
        assert rev["reverted"] is True
        data2 = json.loads(open(cfg).read())
        assert "junk.example" not in data2["search"]["skip_domains"]

    def test_revert_missing_domain_is_noop(self, tmp_path, monkeypatch):
        from lib.optimizer.actions import block_search_domain as b
        monkeypatch.setattr(b, "_CONFIG_FILE", str(tmp_path / "server_config.json"))
        monkeypatch.setattr(b._lib, "reload_config", lambda: None)
        rev = b.revert({"domain": "never.added"})
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
        ]) + "\n")
        monkeypatch.setattr(analyzer, "APP_LOG", str(log))
        out = analyzer._collect_app_log_signals(now - timedelta(hours=1))
        assert out["tool_call_counts"]["web_search"] == 2
        assert out["tool_error_counts"]["fetch_url"] == 1
        assert out["fetch_failure_count"] == 1
        assert out["fetch_timeout_count"] == 1
        assert out["rate_limit_429_count"] == 1

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
            now - timedelta(hours=1), now - timedelta(hours=1), min_count=2)
        labels = {c["fingerprint"] for c in out}
        assert "errorlog::KeyError" in labels
        assert "errorlog::ConnectionError" not in labels  # below threshold

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
        bundle = analyzer.gather_evidence(window_hours=24)
        assert isinstance(bundle, analyzer.EvidenceBundle)
        assert bundle.prior_actions == []  # degraded cleanly
