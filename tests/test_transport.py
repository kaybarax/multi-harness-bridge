import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from multi_harness_bridge.config import register
from multi_harness_bridge.store import Store
from multi_harness_bridge.transport import Principal, dispatch, issue, revoke


@pytest.fixture
def lane(tmp_path):
    project = tmp_path / "orchard"
    project.mkdir()
    config = register("orchard", [project], tmp_path / "state")
    store = Store(config.database)
    credentials = {}
    for harness in ("codex", "devin"):
        for role in ("coordinator", "inbox"):
            session = f"{harness}-{role}"
            store.bind("orchard", harness, role, session)
            token_file = tmp_path / session
            issue(config, Principal("orchard", harness, role, session), token_file)
            credentials[session] = token_file
    return config, store, credentials


def test_credentials_stay_in_project_and_retire(lane):
    config, store, files = lane
    def invoke(session, **command):
        return dispatch(config, files[session].read_text().strip(), command)
    message = invoke("codex-coordinator", op="send", recipient="devin", body="hello", key="hello")
    with pytest.raises(ValueError):
        invoke("codex-coordinator", op="send", project="foreign", recipient="devin", body="x", key="x")
    with pytest.raises(PermissionError):
        invoke("devin-inbox", op="send", recipient="codex", body="x", key="x")
    for role in ("inbox", "coordinator"):
        actor = "devin-" + role
        item = invoke(actor, op="claim")["message"]
        assert item["id"] == message["id"]
        assert invoke(actor, op="finish", id=item["id"], lease=item["lease"])["result"]
    store.bind("orchard", "devin", "inbox", "replacement")
    with pytest.raises(PermissionError):
        invoke("devin-inbox", op="status")
    assert revoke(config, files["codex-coordinator"].read_text().strip()) == 1
    with pytest.raises(PermissionError):
        invoke("codex-coordinator", op="status")


def test_fanout_and_reply_require_bound_coordinator_credentials(lane):
    config, store, files = lane

    def invoke(actor, **command):
        return dispatch(config, files[actor].read_text().strip(), command)

    args = {"op": "send_fanout", "recipients": ["devin"], "body": "Request", "key": "request"}
    with pytest.raises(PermissionError):
        invoke("codex-inbox", **args)
    with pytest.raises(ValueError, match="unexpected"):
        invoke("codex-coordinator", **args, session="devin-coordinator")
    request = invoke("codex-coordinator", **args)
    delivery_id = request["deliveries"][0]["id"]
    for actor in ("codex-inbox", "devin-inbox", "devin-coordinator"):
        item = invoke(actor, op="claim", notification_id=delivery_id)["message"]
        assert item["request_id"] == request["request_id"]
        assert invoke(actor, op="finish", id=item["id"], lease=item["lease"])["result"]
    reply = invoke("devin-coordinator", op="reply_free_text", reply_to=delivery_id, body="Acknowledged", key="reply")
    assert reply["request_id"] == request["request_id"]
    store.bind("orchard", "devin", "coordinator", "replacement")
    with pytest.raises(PermissionError, match="retired"):
        invoke("devin-coordinator", op="reply_free_text", reply_to=delivery_id, body="Acknowledged", key="reply")


def test_a2a_v1_discovery_send_and_host_guard(lane):
    pytest.importorskip("a2a")
    from starlette.testclient import TestClient
    from multi_harness_bridge.a2a_server import application

    config, _, files = lane
    with TestClient(application(config, 9417), base_url="http://127.0.0.1:9417") as client:
        card = client.get("/.well-known/agent-card.json")
        assert card.status_code == 200 and card.json()["name"] == "Multi Harness Bridge"
        assert client.post("/a2a").status_code == 401
        headers = {"Authorization": "Bearer " + files["codex-coordinator"].read_text().strip(), "A2A-Version": "1.0"}
        request = {"jsonrpc": "2.0", "id": "example", "method": "SendMessage", "params": {"message": {
            "messageId": str(uuid4()), "role": "ROLE_USER", "parts": [{"text": json.dumps({
                "op":"send", "recipient":"devin", "body":"hello from A2A", "key":"a2a-test"})}]}}}
        response = client.post("/a2a", headers=headers, json=request)
        assert response.status_code == 200
        reply = response.json()["result"]["message"]
        assert json.loads(reply["parts"][0]["text"])["id"]
        assert client.get("/health", headers={"Origin":"https://example.org"}).status_code == 403
        assert client.get("/health", headers={"Host":"example.org"}).status_code == 403
        assert client.post("/a2a", headers=headers, content=b"x"*65537).status_code == 413


def test_real_stdio_mcp_session(lane):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config, _, files = lane
    async def scenario():
        params = StdioServerParameters(command=sys.executable, args=["-m", "multi_harness_bridge.cli", "mcp",
            "--credential", str(files["codex-coordinator"])], env={**os.environ, "MHB_HOME": str(config.root)})
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            assert {"receive_message", "send_fanout", "reply_free_text"} <= {
                tool.name for tool in (await session.list_tools()).tools}
            result = await session.call_tool("send_message", {"recipient": "devin", "body": "hello", "key": "mcp"})
            assert not result.isError and "id" in json.loads(result.content[0].text)
    asyncio.run(scenario())
