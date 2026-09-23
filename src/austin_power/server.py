from __future__ import annotations

import errno
import logging
import socket
import threading
from contextlib import asynccontextmanager
from typing import Any

import anyio
import anyio.to_thread
import apsw
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from austin_power import __version__, auth, db, store, tokenizer
from austin_power.config import Config, ConfigError, ensure_home, require_loopback

log = logging.getLogger("austin_power.server")


class _Bearer:
    """Wraps the MCP ASGI app so every request but /health needs a valid bearer token."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/health":
            header = dict(scope["headers"]).get(b"authorization", b"").decode("latin-1") or None
            if not auth.check_bearer(header, self.token):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _with_tokenizer_shutdown(lifespan_context):
    """Wrap a Starlette router's lifespan_context so tokenizer.shutdown()
    (kills the kiwi worker child, if any) runs on ASGI shutdown, regardless
    of what its `app` argument turns out to be."""

    @asynccontextmanager
    async def wrapped(app):
        async with lifespan_context(app) as state:
            try:
                yield state
            finally:
                tokenizer.shutdown()

    return wrapped


def build_app(conn: apsw.Connection, token: str, *, port: int):
    """Build the ASGI app: MCP over `/mcp` (bearer-guarded) plus an open `/health`.

    All DB work is serialized through a single lock and run off the event loop
    with `anyio.to_thread.run_sync`, since one apsw connection must not be used
    concurrently from multiple threads/tasks.
    """
    lock = threading.Lock()

    async def run(fn, *args, **kwargs):
        def work():
            with lock:
                return fn(conn, *args, **kwargs)

        try:
            return await anyio.to_thread.run_sync(work)
        except store.ValidationError as e:
            raise ToolError(str(e)) from None
        except apsw.BusyError:
            raise ToolError("storage busy, retry later") from None
        except (apsw.Error, db.TokenizerMismatchError, tokenizer.WorkerError) as e:
            log.exception("storage error")
            raise ToolError(f"storage error: {type(e).__name__}") from None

    srv = MCPServer(
        name="austin-power",
        version=__version__,
        instructions=(
            "Long-term memory for coding agents. search before asking the user; "
            "save durable knowledge."
        ),
    )

    @srv.tool(
        description=(
            "Save a memory. Same (project, title) overwrites. "
            "kind examples: fact, decision, pattern, gotcha, workflow, session."
        ),
        structured_output=True,
    )
    async def save(
        title: str,
        body: str,
        project: str = "",
        kind: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        nid, action = await run(
            store.save, title=title, body=body, project=project, kind=kind, session_id=session_id
        )
        return {"id": nid, "action": action}

    @srv.tool(
        description="Full-text search (Korean-aware). Returns excerpts; use get for the full body.",
        structured_output=True,
    )
    async def search(
        query: str, project: str | None = None, kind: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        return await run(store.search, query, project=project, kind=kind, limit=limit)

    @srv.tool(description="Get one memory with its full body by id.", structured_output=True)
    async def get(id: int) -> dict[str, Any]:
        note = await run(store.get, id)
        if note is None:
            raise ToolError(f"note {id} not found")
        return note

    @srv.tool(
        description="Most recently updated memories, optionally for one project.",
        structured_output=True,
    )
    async def recent(
        project: str | None = None, kind: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        return {"results": await run(store.recent, project=project, kind=kind, limit=limit)}

    @srv.tool(description="Delete a memory by id.", structured_output=True)
    async def forget(id: int) -> dict[str, Any]:
        return {"id": id, "deleted": await run(store.forget, id)}

    @srv.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok", "name": "austin-power", "version": __version__})

    hosts = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    app = srv.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=hosts, allowed_origins=[]),
    )
    # uvicorn's SIGTERM handling runs the ASGI lifespan "shutdown" event
    # before it re-raises the captured signal (see test_serve_lifecycle_and_
    # second_instance_refused's comment) — hook the kiwi worker's shutdown
    # there so `austin-power serve` never leaves an orphaned worker child.
    app.router.lifespan_context = _with_tokenizer_shutdown(app.router.lifespan_context)
    wrapped = _Bearer(app, token)
    wrapped.router = app.router  # tests enter the lifespan via app.router.lifespan_context
    return wrapped


def serve(cfg: Config) -> int:
    """Run the server in the foreground. Returns the process exit code (spec §2.3)."""
    logging.basicConfig(
        level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        require_loopback(cfg.host)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    ensure_home(cfg)
    token = auth.ensure_token(cfg.token_path)
    lock = db.ServerLock(cfg.lock_path)
    if not lock.acquire():
        log.error("another austin-power server is already running for %s", cfg.home)
        return 1
    tokenizer.set_backend("worker", idle_seconds=cfg.kiwi_idle)
    try:
        try:
            conn = db.open_db(cfg.db_path, rebuild_allowed=True)
        except (apsw.Error, db.SchemaTooNewError, OSError) as e:
            log.error("cannot open database %s: %s", cfg.db_path, e)
            return 1
        family = socket.AF_INET6 if ":" in cfg.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((cfg.host, cfg.port))
        except OSError as e:
            sock.close()
            conn.close()
            log.error("port %d is in use (%s)", cfg.port, errno.errorcode.get(e.errno, e))
            return 1
        app = build_app(conn, token, port=cfg.port)
        log.info("listening on %s", cfg.mcp_url)
        uvicorn.Server(
            uvicorn.Config(app, log_level=cfg.log_level.lower(), lifespan="on")
        ).run(sockets=[sock])
        conn.close()
        return 0
    finally:
        tokenizer.shutdown()
        lock.release()
