"""Mage (REST) и Vilva (MCP) против локальных фейковых серверов."""
from __future__ import annotations

import asyncio
import json

from aiohttp import web

from bot.providers.base import ProviderError
from bot.providers.mage import MageProvider
from bot.providers.util import find_first_url, find_key
from bot.providers.vilva import McpClient, VilvaProvider, _parse_sse


async def _serve(app, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await fn(f"http://127.0.0.1:{port}")
    finally:
        await runner.cleanup()


# --- util ---

def test_find_url_prefers_suffix_and_parses_json_text():
    data = [{"type": "text", "text": json.dumps({"thumb": "https://c/t.jpg", "video": "https://c/v.mp4"})}]
    assert find_first_url(data, (".mp4",)) == "https://c/v.mp4"
    assert find_key(data, "video") == "https://c/v.mp4"


def test_parse_sse_picks_matching_id():
    text = 'event: message\ndata: {"jsonrpc":"2.0","method":"x"}\n\nevent: message\ndata: {"jsonrpc":"2.0","id":2,"result":{}}\n\n'
    assert _parse_sse(text, 2)["id"] == 2


# --- mage ---

def mage_app(final="completed"):
    state = {"polls": 0, "body": None, "auth": None}

    async def submit(request):
        state["auth"] = request.headers["Authorization"]
        state["body"] = await request.json()
        return web.json_response({"request_id": "r1", "status": "queued"})

    async def status(request):
        state["polls"] += 1
        if state["polls"] < 2:
            return web.json_response({"status": "processing"})
        return web.json_response({"status": final, "result": {"url": "https://cdn.mage/x.mp4"}})

    app = web.Application()
    app.router.add_post("/v1/generate", submit)
    app.router.add_get("/v1/requests/{id}", status)
    return app, state


def test_mage_video_flow():
    app, state = mage_app()

    async def go(base):
        p = MageProvider("mage_sk_x", base_url=base, poll_interval=0.01)
        try:
            return await p.generate_video("волны", image=b"\xff")
        finally:
            await p.close()

    media = asyncio.run(_serve(app, go))
    assert media.url == "https://cdn.mage/x.mp4"
    assert state["auth"] == "Bearer mage_sk_x"
    cfg = state["body"]["config"]
    assert state["body"]["architecture"] == "cherry" and cfg["model_id"] == "cherry-mini"
    assert cfg["image"].startswith("data:image/jpeg;base64,")


def test_mage_failed():
    app, _ = mage_app(final="failed")

    async def go(base):
        p = MageProvider("k", base_url=base, poll_interval=0.01)
        try:
            await p.generate_image("x")
        except ProviderError:
            return "raised"
        finally:
            await p.close()

    assert asyncio.run(_serve(app, go)) == "raised"


# --- vilva ---

def vilva_app():
    state = {"calls": [], "polls": 0, "session_ok": True}

    def sse(msg):
        return web.Response(text=f"event: message\ndata: {json.dumps(msg)}\n\n", content_type="text/event-stream")

    def tool_result(rid, payload):
        return sse({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}})

    async def handler(request):
        msg = await request.json()
        method = msg.get("method")
        if method == "initialize":
            return web.json_response(
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                headers={"Mcp-Session-Id": "s1"},
            )
        if request.headers.get("Mcp-Session-Id") != "s1":
            state["session_ok"] = False
        if method == "notifications/initialized":
            return web.Response(status=202)
        name = msg["params"]["name"]
        state["calls"].append((name, msg["params"]["arguments"]))
        if name == "generate_image":
            return tool_result(msg["id"], {"imageUrl": "https://cdn.vilva/i.png"})
        if name == "generate_video":
            return tool_result(msg["id"], {"generationId": "g1", "status": "processing"})
        if name == "check_generation":
            state["polls"] += 1
            if state["polls"] < 2:
                return tool_result(msg["id"], {"status": "processing"})
            return tool_result(msg["id"], {"status": "completed", "url": "https://cdn.vilva/v.mp4"})
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}})

    app = web.Application()
    app.router.add_post("/mcp", handler)
    return app, state


def test_vilva_image_and_video():
    app, state = vilva_app()

    async def go(base):
        p = VilvaProvider("vk_live_x", url=base + "/mcp", poll_interval=0.01)
        try:
            img = await p.generate_image("кот")
            vid = await p.generate_video("волны")
            return img, vid
        finally:
            await p.close()

    img, vid = asyncio.run(_serve(app, go))
    assert img.url == "https://cdn.vilva/i.png"
    assert vid.url == "https://cdn.vilva/v.mp4"
    assert state["session_ok"]
    assert [c[0] for c in state["calls"]] == ["generate_image", "generate_video", "check_generation", "check_generation"]


def test_vilva_tool_error_raises():
    async def handler(request):
        msg = await request.json()
        if msg.get("method") == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        if "id" not in msg:
            return web.Response(status=202)
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"],
                                  "result": {"isError": True, "content": [{"type": "text", "text": "no credits"}]}})

    app = web.Application()
    app.router.add_post("/mcp", handler)

    async def go(base):
        c = McpClient(base + "/mcp", "k")
        try:
            await c.call_tool("generate_image", {"prompt": "x"})
        except ProviderError as e:
            return str(e)
        finally:
            await c.close()

    assert "no credits" in asyncio.run(_serve(app, go))
