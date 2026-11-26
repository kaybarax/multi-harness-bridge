import pytest

from multi_harness_bridge import pickup, wake_policy

PULL_MODULES = {"test_pickup", "test_watcher"}
# Suites that specify the wake policy itself use its shipped defaults.
POLICY_MODULES = {"test_wake_policy"}
HOST_MARKERS = ("CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "DEVIN_SESSION_ID",
                "DEVIN_LOCAL_SESSION_ID", "MHB_ANTIGRAVITY_EVENT", "MULTI_HARNESS_BRIDGE_HARNESS",
                "MHB_CLAUDE_HOOK_CONTEXT")


@pytest.fixture(autouse=True)
def _no_host_session_leaks_in(monkeypatch):
    """Tests run as a plain terminal unless a test sets a host's markers itself."""
    for name in HOST_MARKERS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _push_by_default_for_existing_suites(request, monkeypatch):
    """Earlier suites specify the original push flow; they must pass unchanged under push.

    The pull suites use the shipped default (pull for hosts that support it).
    """
    module = request.module.__name__.rsplit(".", 1)[-1]
    if module not in PULL_MODULES | POLICY_MODULES:
        monkeypatch.setitem(pickup.DEFAULTS, "coordinator_pickup", pickup.PUSH)
    if module not in POLICY_MODULES:
        # Earlier suites specify the acknowledge-only wake and send freely.
        monkeypatch.setitem(wake_policy.DEFAULTS, "wake_actions", wake_policy.ACKNOWLEDGE)
        monkeypatch.setitem(wake_policy.DEFAULTS, "loop_cap_messages", 1000)
