import pytest

from multi_harness_bridge import pickup

PULL_MODULES = {"test_pickup", "test_watcher"}


@pytest.fixture(autouse=True)
def _push_by_default_for_existing_suites(request, monkeypatch):
    """Earlier suites specify the original push flow; they must pass unchanged under push.

    The pull suites use the shipped default (pull for hosts that support it).
    """
    if request.module.__name__.rsplit(".", 1)[-1] not in PULL_MODULES:
        monkeypatch.setitem(pickup.DEFAULTS, "coordinator_pickup", pickup.PUSH)
