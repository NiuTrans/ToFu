#!/usr/bin/env python3
"""Two-tab coordination contract: 模型 registry page vs 服务商 supply page.

The settings runtime splits model-routing presentation across two mirror
owners:

- ``settings/model_catalog_tab.js`` owns ``#settingsTab_models`` — the
  global, read-only Creator/Model registry. It must never paint provider
  supply (``stgProviderList``) or define the Providers renderer.
- ``settings/provider_render.js`` owns the Providers tab — provider supply
  only. It must never paint the model catalogue (``stgModelCatalog``) or
  define the Models renderer.

Cross-page freshness is structural, not conventional: ``switchSettingsTab``
repaints the read-only models tab on activation, so a flow that mutates
creators/models but only calls ``_renderProvidersTab()`` cannot leave the
registry page stale. (The Providers tab is deliberately excluded from
repaint-on-activation — its cards hold unsaved credential input.)

Each render entry point has exactly one definition across all retained
sections; callers reference them by name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests._runtime_sections import _section_source as retained_source

pytestmark = pytest.mark.unit

MODELS_SECTION = 'frontend/src/features/settings/model-catalog-tab.ts'
PROVIDERS_SECTION = 'settings/provider_render.js'
CORE_SECTION = 'frontend/src/features/settings/core-panel.ts'


def _section_source(name):
    return Path(name).read_text() if name.startswith('frontend/') else retained_source(name)


def test_models_tab_never_touches_provider_supply() -> None:
    source = _section_source(MODELS_SECTION)
    assert 'stgProviderList' not in source
    assert 'function _renderProvidersTab' not in source


def test_providers_tab_never_touches_model_catalog() -> None:
    source = _section_source(PROVIDERS_SECTION)
    assert 'stgModelCatalog' not in source
    assert 'function _renderModelCatalogTab' not in source
    assert 'function _renderModelRoutingTabs' not in source


def test_render_entry_points_have_exactly_one_definition() -> None:
    for source, symbol in ((MODELS_SECTION, 'renderModelRoutingTabs'),
                           (MODELS_SECTION, 'renderModelCatalogTab'),
                           (PROVIDERS_SECTION, '_renderProvidersTab')):
        assert _section_source(source).count(f'function {symbol}(') == 1


def test_models_tab_repaints_on_activation() -> None:
    """The stale-registry class is dead structurally, not by convention."""
    source = _section_source(CORE_SECTION)
    switch_body = re.search(
        r'function switchSettingsTab\(tabId: string\): void \{(?P<body>.*?)\n\}',
        source,
        re.DOTALL,
    )
    assert switch_body, 'switchSettingsTab not found'
    body = switch_body.group('body')
    assert "tabId === 'models'" in body
    assert '_renderModelCatalogTab();' in body


def test_providers_tab_does_not_repaint_on_activation() -> None:
    """Provider cards hold unsaved credential input; activation must not
    rebuild them."""
    source = _section_source(CORE_SECTION)
    switch_body = re.search(
        r'function switchSettingsTab\(tabId: string\): void \{(?P<body>.*?)\n\}',
        source,
        re.DOTALL,
    )
    assert switch_body, 'switchSettingsTab not found'
    assert '_renderProvidersTab' not in switch_body.group('body')


def test_model_catalog_projection_excludes_provider_fields() -> None:
    """The Creator/Model-only projection is a data boundary: the staged
    authority's provider-side arrays never reach the Models page owner."""
    source = _section_source(MODELS_SECTION)
    projection = re.search(
        r'(?:var|const) catalogDocument = \{(?P<body>.*?)\n  \};',
        source,
        re.DOTALL,
    )
    assert projection, 'catalogDocument projection not found'
    body = projection.group('body')
    for supply_field in ('providers', 'connections', 'credentials',
                         'offerings', 'deployments', 'aliases', 'routes'):
        assert f'_stgModelRouting.{supply_field}' not in body


def test_models_tab_only_projects_registered_models() -> None:
    """The Model registry excludes persisted non-callable research drafts."""
    source = _section_source(MODELS_SECTION)
    assert 'model.registration_state === \'ready\'' in source
    assert '!model.registration_state' in source  # legacy migration compatibility
    assert 'models: (routing.models || []).filter(' in source


def test_model_catalog_projection_forwards_derived_pricing() -> None:
    """Catalogue sync is insert-only: rows inserted before a price was known
    carry no ``list_pricing``. The backend stamps ``official_pricing`` at
    the read boundary; if this projection dropped it, those rows would stay
    price-less in the browser forever even though the official table covers
    them."""
    source = _section_source(MODELS_SECTION)
    projection = re.search(
        r'(?:var|const) catalogDocument = \{(?P<body>.*?)\n  \};',
        source,
        re.DOTALL,
    )
    assert projection, 'catalogDocument projection not found'
    body = projection.group('body')
    assert 'model.official_pricing' in body
    assert 'official_pricing:' in body
