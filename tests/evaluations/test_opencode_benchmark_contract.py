"""Offline security and version compatibility checks for the OpenCode arm."""
import pytest
from evaluations.opencode_contract import guest_command, guest_config

pytestmark = pytest.mark.unit


def config(url="http://10.0.2.101:8080", **kwargs):
    return guest_config(proxy_url=url, model="glm-5.3-m17",
                        context_tokens=200000, output_tokens=32768, **kwargs)


def test_same_model_for_main_and_small_requests():
    value = config()
    assert value["model"] == value["small_model"] == "benchmark/glm-5.3-m17"
    assert value["enabled_providers"] == ["benchmark"]
    provider = value["provider"]["benchmark"]
    assert provider["options"] == {"baseURL": "http://10.0.2.101:8080/v1"}
    assert value["share"] == "disabled"
    assert value["autoupdate"] is False


@pytest.mark.parametrize("url", [
    "https://10.0.2.101:8080", "http://127.0.0.1:8080",
    "http://example.com:8080", "http://10.0.2.101", "http://10.0.2.101:0",
    "http://user:secret@10.0.2.101:8080", "http://10.0.2.101:8080/private",
    "http://10.0.2.101:8080?key=secret", "http://10.0.2.101:8080#fragment",
    "http://10.0.2.101:99999",
])
def test_rejects_non_control_endpoints(url):
    with pytest.raises(ValueError):
        config(url)


@pytest.mark.parametrize("context,output", [(True, 1), (100, True), (100, 100),
                                          (100, 0), (2000001, 32768)])
def test_rejects_invalid_budgets(context, output):
    with pytest.raises(ValueError):
        guest_config(proxy_url="http://10.0.2.101:8080", model="glm-5.3-m17",
                     context_tokens=context, output_tokens=output)


def test_current_cli_argv_preserves_instruction_without_shell_interpolation():
    prompt = "$(touch /tmp/unwanted) 'quoted'"
    argv = guest_command(binary="/var/tmp/tofu-opencode/opencode",
                         model="glm-5.3-m17", instruction=prompt)
    assert argv[-2:] == ["--", prompt]
    assert "--auto" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--thinking" not in argv
    assert "--pure" in argv


def test_rejects_host_binary():
    with pytest.raises(ValueError):
        guest_command(binary="/usr/bin/opencode", model="glm-5.3-m17", instruction="task")
