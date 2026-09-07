"""Behavior contract for metadata-only typed conversation startup."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/conversation/application/conversation-startup.ts'
OWNER_BUNDLE = native_module_path('.native/conversation-startup.js', OWNER)


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_startup_coordinates_metadata_failures_and_active_presentation():
    harness = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);

function createPorts(overrides = {}) {
  const calls = {
    catalog: 0, folders: 0, migrated: 0, retries: 0,
    folderWarnings: 0, catalogWarnings: 0,
    streaming: [], renders: [], pending: [], hydrate: 0, dispatch: 0,
  };
  let activeId = null;
  let active = null;
  let busy = false;
  const ports = {
    loadConversationCatalog: async () => { calls.catalog += 1; },
    loadFolders: async () => { calls.folders += 1; },
    migratePinnedToFolder: () => { calls.migrated += 1; },
    scheduleFolderLoadRetry: () => { calls.retries += 1; },
    hasTurnHydrator: () => true,
    activeConversationId: () => activeId,
    activeConversation: () => active,
    isConversationBusy: () => busy,
    showStreamingPresentation: (id) => calls.streaming.push(id),
    requestAuthoritativeRender: (id) => calls.renders.push(id),
    renderPendingQueue: (id) => calls.pending.push(id),
    warnFolderLoad: () => { calls.folderWarnings += 1; },
    warnCatalogLoad: () => { calls.catalogWarnings += 1; },
    ...overrides,
  };
  return {
    calls, ports,
    setActive(id, conversation, isBusy) {
      activeId = id; active = conversation; busy = isBusy;
    },
  };
}

(async () => {
  const metadataOnly = createPorts();
  await createConversationStartup(metadataOnly.ports).initialize();
  check('metadata_sources_load_once',
    metadataOnly.calls.catalog === 1 && metadataOnly.calls.folders === 1);
  check('successful_folder_load_migrates_pins',
    metadataOnly.calls.migrated === 1 && metadataOnly.calls.retries === 0);
  check('startup_interface_cannot_hydrate_or_dispatch',
    !('hydrateConversation' in metadataOnly.ports)
      && !('startAssistantResponse' in metadataOnly.ports));

  const catalogFailure = createPorts({
    loadConversationCatalog: async () => { throw new Error('catalog offline'); },
  });
  await createConversationStartup(catalogFailure.ports).initialize();
  check('folders_survive_catalog_failure',
    catalogFailure.calls.folders === 1
      && catalogFailure.calls.migrated === 1
      && catalogFailure.calls.catalogWarnings === 1);

  const folderFailure = createPorts({
    loadFolders: () => { throw new Error('folders offline'); },
  });
  await createConversationStartup(folderFailure.ports).initialize();
  check('folder_failure_retries_without_blocking_catalog',
    folderFailure.calls.catalog === 1
      && folderFailure.calls.folderWarnings === 1
      && folderFailure.calls.retries === 1);

  let releaseFolder;
  let initialized = false;
  const deferredFolder = createPorts({
    loadFolders: () => new Promise((resolve) => { releaseFolder = resolve; }),
  });
  const pendingInitialization = createConversationStartup(
    deferredFolder.ports,
  ).initialize().then(() => { initialized = true; });
  await Promise.resolve(); await Promise.resolve();
  check('catalog_runs_while_folder_load_is_pending',
    deferredFolder.calls.catalog === 1 && initialized === false);
  releaseFolder();
  await pendingInitialization;
  check('initialization_awaits_folder_completion', initialized === true);

  const presentation = createPorts();
  presentation.setActive('conv-1', { id: 'conv-1' }, true);
  const controller = createConversationStartup(presentation.ports);
  controller.ensureActivePresentation();
  check('busy_active_conversation_uses_streaming_surface',
    presentation.calls.streaming.join(',') === 'conv-1'
      && presentation.calls.renders.length === 0
      && presentation.calls.pending.join(',') === 'conv-1');
  presentation.setActive('conv-1', { id: 'conv-1' }, false);
  controller.ensureActivePresentation();
  check('idle_active_conversation_requests_authoritative_render',
    presentation.calls.renders.join(',') === 'conv-1'
      && presentation.calls.pending.length === 2);

  const missingHydrator = createPorts({ hasTurnHydrator: () => false });
  await createConversationStartup(missingHydrator.ports).initialize();
  check('missing_turn_runtime_is_visible',
    missingHydrator.calls.catalogWarnings === 1);

  console.log(checks.join('\n'));
  if (checks.some((line) => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch((error) => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        ['node', '-e', harness, OWNER_BUNDLE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout or '') + (result.stderr or '')
    assert result.returncode == 0, output
    assert output.count('PASS') == 10, output
