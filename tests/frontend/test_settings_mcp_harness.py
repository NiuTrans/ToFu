"""MCP scenario drivers require settlement beyond Node's successful exit."""
import pytest

from tests.frontend import test_settings_mcp_commands as commands
from tests.frontend import test_settings_mcp_reads as reads
from tests.frontend.test_settings_import_oauth_lifecycle import settings_oauth_bundle as settings_oauth_bundle

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('module,driver', [
    (commands, commands.test_mcp_command_lifetime),
    (reads, reads.test_settings_mcp_read_lifetime),
])
def test_mcp_driver_rejects_unsettled_node_success(monkeypatch, settings_oauth_bundle, module, driver):
    # Node exits zero here: an unresolved Promise does not keep its event loop alive.
    # Exercise each real driver so that zero exit cannot silently certify the scenario.
    monkeypatch.setattr(module, 'HARNESS', 'void (async () => { await new Promise(() => {}); })();')
    with pytest.raises(AssertionError, match='scenario still unsettled'):
        driver(settings_oauth_bundle, 'unsettled')
