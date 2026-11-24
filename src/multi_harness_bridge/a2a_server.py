"""A2A v1 JSON-RPC interface to the role-scoped local mailbox."""
import asyncio
import json
from contextvars import ContextVar

from .transport import authenticate, dispatch, verify_role
from .config import open_store


def application(config, port):
    from a2a.helpers import get_message_text, new_text_message
    from a2a.server.agent_execution import AgentExecutor
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
    from a2a.server.tasks import InMemoryTaskStore
    from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill,
                           HTTPAuthSecurityScheme, SecurityRequirement, SecurityScheme)
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    bearer = ContextVar("mailbox_bearer")

    class Executor(AgentExecutor):
        async def execute(self, context, event_queue):
            try:
                command = json.loads(get_message_text(context.message))
                result = await asyncio.to_thread(dispatch, config, bearer.get(), command)
            except (ValueError, TypeError, PermissionError, RuntimeError) as exc:
                result = {"error": str(exc)}
            await event_queue.enqueue_event(new_text_message(json.dumps(result)))

        async def cancel(self, context, event_queue):
            raise NotImplementedError("Mailbox operations complete synchronously")

    card = AgentCard(
        name="Multi Harness Bridge", version="1.4.1",
        description="Project-scoped local coordination mailbox. Send JSON operations in a text part.",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain"], default_output_modes=["text/plain"],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", protocol_version="1.0",
                                             url=f"http://127.0.0.1:{port}/a2a")],
        security_schemes={"roleBearer": SecurityScheme(
            http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Bearer"))},
        security_requirements=[SecurityRequirement(schemes={"roleBearer": {"list": []}})],
        skills=[AgentSkill(id="mailbox", name="Mailbox", description="send, send_fanout, reply_free_text, claim, finish, status, stop, resume",
                           tags=["coordination"])])
    handler = DefaultRequestHandler(agent_executor=Executor(), task_store=InMemoryTaskStore(), agent_card=card)

    async def health(_request):
        return JSONResponse({"service": "multi-harness-bridge"})

    app = Starlette(routes=[Route("/health", health), *create_agent_card_routes(card),
                            *create_jsonrpc_routes(handler, "/a2a")])

    async def guarded(scope, receive, send):
        if scope["type"] != "http":
            return await app(scope, receive, send)
        headers = dict(scope["headers"])
        allowed_hosts = {f"localhost:{port}".encode(), f"127.0.0.1:{port}".encode()}
        if headers.get(b"host") not in allowed_hosts or b"origin" in headers:
            return await JSONResponse({"error": "Local client required"}, 403)(scope, receive, send)
        if scope["method"] == "GET" and scope["path"] in ("/health", "/.well-known/agent-card.json"):
            return await app(scope, receive, send)
        scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
        try:
            if scheme.lower() != "bearer":
                raise PermissionError()
            verify_role(open_store(config), authenticate(config, token))
        except (PermissionError, ValueError):
            return await JSONResponse({"error": "Unauthorized"}, 401)(scope, receive, send)
        total = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            total.extend(event.get("body", b""))
            if len(total) > 65536:
                return await JSONResponse({"error": "Request too large"}, 413)(scope, receive, send)
            if not event.get("more_body"):
                break
        body = bytes(total)

        async def replay():
            nonlocal body
            if body is None:
                return await receive()
            chunk, body = body, None
            return {"type": "http.request", "body": chunk, "more_body": False}

        context_token = bearer.set(token)
        try:
            await app(scope, replay, send)
        finally:
            bearer.reset(context_token)

    return guarded
