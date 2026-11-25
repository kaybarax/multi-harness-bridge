"""Stream mailbox arrival hints into an existing native chat subscription."""
import json
import time
from pathlib import Path


def subscribe(config, store, project, harness, role, session, *, output, interval=1.0, wait=time.sleep):
    """Exit on stop or retirement. Never claim mail on the chat's behalf."""
    if interval <= 0 or interval > 5:
        raise ValueError("Monitor interval must be greater than zero and at most five seconds")
    config.resolve(Path.cwd(), project)
    seen = set()
    while True:
        if store.roles(project, harness).get(role) != session or not store.status(project, harness)["enabled"]:
            return
        available = store.pending_notifications(project, harness, role, session)
        for message_id, attempt in available:
            if (message_id, attempt) not in seen:
                line = {"project": project, "harness": harness, "role": role, "session": session, "id": message_id}
                from . import wake_policy
                wake_policy.record_hint(config.root, project, harness, session, [message_id])
                print(json.dumps(line), file=output, flush=True)
        seen = set(available)
        wait(interval)
