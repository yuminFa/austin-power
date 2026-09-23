import anyio
import httpx
import pytest

from austin_power import db, server

pytestmark = pytest.mark.kiwi

TOKEN = "t0ken"
H = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
    "Host": "127.0.0.1:7760",
}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


@pytest.fixture
def app(tmp_path):
    return server.build_app(db.open_db(tmp_path / "m.db"), TOKEN, port=7760)


async def call(app, body, headers=H, auth=True):
    hdrs = dict(headers)
    if auth:
        hdrs["Authorization"] = f"Bearer {TOKEN}"
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7760"
    ) as c:
        return await c.post("/mcp", json=body, headers=hdrs)


def tool(app, name, args):
    r = anyio.run(call, app, rpc("tools/call", {"name": name, "arguments": args}))
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_unauthorized(app):
    r = anyio.run(call, app, rpc("tools/list"), H, False)
    assert r.status_code == 401 and "www-authenticate" not in r.headers


def test_bad_host(app):
    r = anyio.run(call, app, rpc("tools/list"), {**H, "Host": "evil.example:7760"})
    assert r.status_code == 421


def test_health_open(app):
    async def go():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7760"
        ) as c:
            return await c.get("/health", headers={"Host": "127.0.0.1:7760"})

    r = anyio.run(go)
    assert r.status_code == 200 and r.json()["name"] == "austin-power" and "count" not in r.json()


def test_tools_listed(app):
    r = anyio.run(call, app, rpc("tools/list"))
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "save",
        "search",
        "get",
        "recent",
        "forget",
    }


def test_call_without_initialize_roundtrip(app):
    # Both calls share one lifespan entry (as a real long-lived server would):
    # mcp's StreamableHTTPSessionManager.run() may only be entered once per
    # app instance, even in stateless mode (its task group backs every request).
    async def go():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7760"
        ) as c:
            r1 = await c.post(
                "/mcp",
                json=rpc(
                    "tools/call",
                    {
                        "name": "save",
                        "arguments": {"title": "배포", "body": "배포 절차를 정리했다", "project": "p"},
                    },
                ),
                headers={**H, "Authorization": f"Bearer {TOKEN}"},
            )
            r2 = await c.post(
                "/mcp",
                json=rpc("tools/call", {"name": "search", "arguments": {"query": "배포가", "project": "p"}}),
                headers={**H, "Authorization": f"Bearer {TOKEN}"},
            )
            return r1, r2

    r1, r2 = anyio.run(go)
    assert r1.status_code == 200 and r2.status_code == 200
    res1, res2 = r1.json()["result"], r2.json()["result"]
    assert res1["isError"] is False and res1["structuredContent"]["action"] == "created"
    assert res2["structuredContent"]["results"][0]["title"] == "배포"


def test_validation_error_is_tool_error(app):
    res = tool(app, "save", {"title": "", "body": "b"})
    assert res["isError"] is True and "invalid title" in res["content"][0]["text"]


def test_get_missing(app):
    res = tool(app, "get", {"id": 999})
    assert res["isError"] is True and "note 999 not found" in res["content"][0]["text"]
