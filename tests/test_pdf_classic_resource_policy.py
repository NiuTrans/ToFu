"""Launch-derived budgets for local non-VLM PDF parsing."""

from __future__ import annotations

import pytest

from lib.pdf_parser.policy import (
    bounded_pdf_image_count,
    bounded_pdf_image_width,
    bounded_pdf_pages,
    bounded_pdf_text_chars,
    classic_pdf_worker_idle_seconds,
    resolve_classic_pdf_budget,
)
from runtime_guards import SystemResourceSnapshot, deployment_resource_default


pytestmark = pytest.mark.unit
_MIB = 1024 * 1024


def _environment(**overrides: str) -> dict[str, str]:
    values = {
        'TOFU_PDF_PROCESSES': '1',
        'TOFU_PDF_PARSE_CAPACITY': '3',
        'TOFU_PDF_MAX_PAGES': '512',
        'TOFU_PDF_MAX_TEXT_MIB': '4',
        'TOFU_PDF_PARSE_TIMEOUT': '1024',
        'TOFU_PDF_WORKER_IDLE_SECONDS': '60',
    }
    values.update(overrides)
    return values


def test_reference_budget_is_finite_across_every_multiplier():
    budget = resolve_classic_pdf_budget(_environment())

    assert budget.processes == 1
    assert budget.unfinished_capacity == 3
    assert budget.max_pages == 512
    assert budget.max_text_chars == 4 * _MIB
    assert budget.timeout_seconds == 1024
    assert budget.worker_idle_seconds == 60


def test_operator_overrides_remain_inside_hard_ceilings():
    budget = resolve_classic_pdf_budget(_environment(
        TOFU_PDF_PROCESSES='999999',
        TOFU_PDF_PARSE_CAPACITY='999999',
        TOFU_PDF_MAX_PAGES='999999',
        TOFU_PDF_MAX_TEXT_MIB='999999',
        TOFU_PDF_PARSE_TIMEOUT='999999',
    ))

    assert budget.processes == 16
    assert budget.unfinished_capacity == 64
    assert budget.max_pages == 4_096
    assert budget.max_text_chars == 64 * _MIB
    assert budget.timeout_seconds == 3_600
    assert classic_pdf_worker_idle_seconds(_environment(
        TOFU_PDF_WORKER_IDLE_SECONDS='999999')) == 86_400


def test_explicit_zero_keeps_classic_pool_resident():
    assert classic_pdf_worker_idle_seconds(_environment(
        TOFU_PDF_WORKER_IDLE_SECONDS='0')) == 0.0


def test_process_cache_key_tracks_runtime_environment_changes(monkeypatch):
    for name, value in _environment().items():
        monkeypatch.setenv(name, value)
    assert resolve_classic_pdf_budget().max_pages == 512

    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '37')

    assert resolve_classic_pdf_budget().max_pages == 37


def test_unfinished_capacity_cannot_fall_below_process_count():
    budget = resolve_classic_pdf_budget(_environment(
        TOFU_PDF_PROCESSES='8',
        TOFU_PDF_PARSE_CAPACITY='1',
    ))

    assert budget.processes == 8
    assert budget.unfinished_capacity == 8


@pytest.mark.parametrize('requested', [0, -1, None, 'bad'])
def test_nonpositive_or_malformed_request_uses_policy_default(requested):
    budget = resolve_classic_pdf_budget(_environment())

    assert bounded_pdf_pages(requested, budget) == 512
    assert bounded_pdf_text_chars(requested, budget) == 4 * _MIB


def test_request_can_lower_but_never_raise_policy_ceiling():
    budget = resolve_classic_pdf_budget(_environment())

    assert bounded_pdf_pages(20, budget) == 20
    assert bounded_pdf_pages(10_000, budget) == 512
    assert bounded_pdf_text_chars(8_000, budget) == 8_000
    assert bounded_pdf_text_chars(100 * _MIB, budget) == 4 * _MIB


def test_image_projection_dimensions_are_hard_bounded():
    assert bounded_pdf_image_count(0) == 0
    assert bounded_pdf_image_count(20) == 20
    assert bounded_pdf_image_count(999_999) == 64
    assert bounded_pdf_image_width('bad') == 1_024
    assert bounded_pdf_image_width(900) == 900
    assert bounded_pdf_image_width(999_999) == 2_048


def test_eight_gib_reference_probe_keeps_long_text_without_vlm_multiplier():
    snapshot = SystemResourceSnapshot(
        host_cpu_count=8,
        affinity_cpu_count=8,
        cgroup_cpu_count=None,
        effective_cpu_count=8,
        host_memory_total_mb=8_192,
        host_memory_available_mb=4_096,
        cgroup_memory_limit_mb=None,
        cgroup_memory_current_mb=None,
        effective_memory_capacity_mb=8_192,
        effective_memory_available_mb=4_096,
        disk_total_mb=500 * 1_024,
        disk_free_mb=250 * 1_024,
    )

    resolved = {
        name: deployment_resource_default(name, {}, snapshot=snapshot)
        for name in (
            'TOFU_PDF_PROCESSES',
            'TOFU_PDF_PARSE_CAPACITY',
            'TOFU_PDF_MAX_PAGES',
            'TOFU_PDF_MAX_TEXT_MIB',
            'TOFU_PDF_PARSE_TIMEOUT',
            'TOFU_PDF_WORKER_IDLE_SECONDS',
        )
    }

    assert resolved == {
        'TOFU_PDF_PROCESSES': 1,
        'TOFU_PDF_PARSE_CAPACITY': 3,
        'TOFU_PDF_MAX_PAGES': 512,
        'TOFU_PDF_MAX_TEXT_MIB': 4,
        'TOFU_PDF_PARSE_TIMEOUT': 1_024,
        'TOFU_PDF_WORKER_IDLE_SECONDS': 60,
    }
