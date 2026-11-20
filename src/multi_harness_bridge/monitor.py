"""Stream mailbox arrival hints into an existing native chat subscription."""
import json
import time
from pathlib import Path


def subscribe(config, store, project, harness, role, session, *, output, interval=1.0, wait=time.sleep):
    """Exit on stop or retirement. Never claim mail on the chat's behalf."""
    if interval <= 0 or interval > 5:
        raise ValueError("Monitor interval must be greater than zero and at most five seconds")
    config.resolve(Path.cwd(), project)
    last = None
    while True:
        if store.roles(project, harness).get(role) != session or not store.status(project, harness)["enabled"]:
            return
        item = store.pending(project, harness, role, session)
        if item:
            marker = (item["id"], item["attempt"])
            if marker != last:
                line = {"project": project, "harness": harness, "role": role, "session": session, "id": item["id"]}
                print(json.dumps(line), file=output, flush=True)
                last = marker
        wait(interval)
