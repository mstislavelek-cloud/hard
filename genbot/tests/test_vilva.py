"""Vilva: MCP-клиент, генерация, модели и агент против фейкового MCP-сервера."""
from __future__ import annotations

import asyncio
import json

from aiogram.methods import SendMessage, SendPhoto, SendVideo
from aiohttp import web

from bot.handlers import BTN_AGENT
from bot.providers.vilva import AgentState, McpClient, VilvaProvider, _parse_sse, build_args
from tests.conftest import serve, sse

SCHEMAS = {
    "list_models": {"type": "object", "properties": {}},
    "generate_image": {"type": "object", "properties": {
        "prompt": {"type": "string"}, "modelId": {"type": "string"},
        "aspectRatio": {"type": "string", "enum": ["1:1", "16:9", "9:16"]}, "imageUrl": {"type": "string"}}},
    "generate_video": {"type": "object", "properties": {
        "prompt": {"type": "string"}, "modelId": {"type": "string"},
        "duration": {"type": "integer", "enum": [5, 10]}, "imageUrl": {"type": "string"}}},
    "check_generation": {"type": "object", "properties": {"generationId": {"type": "string"}}},
    "agent_create_run": {"type": "object", "properties": {
        "brief": {"type": "string"}, "mode": {"type": "string"}, "creditBudget": {"type": "number"}}},
    "agent_get_run": {"type": "object", "properties": {"runId": {"type": "string"}}},
    "agent_get_plan": {"type": "object", "properties": {"runId": {"type": "string"}, "action": {"type": "string"}}},
    "agent_respond": {"type": "object", "properties": {"runId": {"type": "string"}, "response": {"type": "string"}}},
    "agent_cancel_run": {"type": "object", "properties": {"runId": {"type": "string"}}},
}

MODELS = {"models": [
    {"id": "flux-pro", "name": "Flux Pro", "type": "image", "credits": 4},
    {"id": "kling-3", "name": "Kling 3", "type": "video", "credits": 40},
    {"id": "eleven", "name": "Voice", "type": "voiceover", "credits": 1},
]}


def vilva_server(agent_script=None):
    """agent_script: список ответов agent_get_run по очереди; после одобрения плана — следующий."""
    state = {"calls": [], "session_ok": True, "polls": 0, "run_polls": 0, "approved": None}
    script = list(agent_script or [])

    def result(rid, data):
        return sse({"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": json.dumps(data)}]}})

    async def handler(request):
        msg = await request.json()
        method = msg.get("method")
        if method == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {"capabilities": {}}},
                                     headers={"Mcp-Session-Id": "s1"})
        if request.headers.get("Mcp-Session-Id") != "s1":
            state["session_ok"] = False
        if method == "notifications/initialized":
            return web.Response(status=202)
        if method == "tools/list":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": n, "inputSchema": s} for n, s in SCHEMAS.items()]}})
        name, args = msg["params"]["name"], msg["params"]["arguments"]
        state["calls"].append((name, args))
        rid = msg["id"]
        if name == "list_models":
            return result(rid, MODELS)
        if name == "generate_image":
            return result(rid, {"imageUrl": "https://cdn.vilva/i.png", "creditsUsed": 4})
        if name == "generate_video":
            return result(rid, {"generationId": "g1", "status": "processing"})
        if name == "check_generation":
            state["polls"] += 1
            if state["polls"] < 2:
                return result(rid, {"status": "processing"})
            return result(rid, {"status": "completed", "url": "https://cdn.vilva/v.mp4", "creditsUsed": 40})
        if name == "agent_create_run":
            return result(rid, {"runId": "run-1", "status": "queued"})
        if name == "agent_get_run":
            state["run_polls"] += 1
            if len(script) > 1 and not (script[0].get("status") == "awaiting_approval" and state["approved"] is None):
                return result(rid, script.pop(0))
            return result(rid, script[0])
        if name == "agent_get_plan":
            if "action" in args:
                state["approved"] = args["action"]
                if script and script[0].get("status") == "awaiting_approval":
                    script.pop(0)
                return result(rid, {"ok": True})
            return result(rid, {"steps": [{"title": "5 карточек"}, {"title": "видео-обложка"}], "estimatedCredits": 30})
        if name == "agent_cancel_run":
            script[:] = [{"status": "cancelled"}]
            return result(rid, {"ok": True})
        return web.json_response({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no"}})

    app = web.Application()
    app.router.add_post("/mcp", handler)
    return app, state


# --- без сети ---

def test_build_args_follows_schema():
    schema = SCHEMAS["generate_image"]
    args = build_args(schema, {"prompt": "x", "model": "m", "aspect_ratio": "1:1", "duration": "5"})
    assert args == {"prompt": "x", "modelId": "m", "aspectRatio": "1:1"}
    assert build_args(None, {"run_id": "r"}) == {"runId": "r"}


def test_parse_sse():
    text = 'data: {"jsonrpc":"2.0","method":"x"}\n\ndata: {"jsonrpc":"2.0","id":2,"result":{}}\n\n'
    assert _parse_sse(text, 2)["id"] == 2


def test_agent_state_phases():
    assert AgentState.from_payload("r", {"status": "awaiting_approval"}).phase == "plan"
    assert AgentState.from_payload("r", {"status": "waiting_for_input", "question": "Какой цвет?"}).text == "Какой цвет?"
    done = AgentState.from_payload("r", {"status": "completed", "assets": [
        {"url": "https://c/a.png"}, {"url": "https://c/b.mp4"}], "creditsUsed": 12})
    assert done.phase == "done" and done.credits_used == 12 and len(done.urls) == 2


# --- против сервера ---

def test_generation_and_models():
    app, state = vilva_server()

    async def go(base):
        p = VilvaProvider("vk_live_x", url=base + "/mcp", poll_interval=0.01)
        try:
            specs = await p.discover_models()
            img_spec = next(s for s in specs if s.kind == "image")
            vid_spec = next(s for s in specs if s.kind == "video")
            img = await p.generate(img_spec, "кот", {"aspect_ratio": "16:9"}, b"\xff")
            vid = await p.generate(vid_spec, "волны", {"duration": "10"}, None)
            return specs, img, vid
        finally:
            await p.close()

    specs, img, vid = asyncio.run(serve(app, go))
    assert {s.key for s in specs} == {"vilva:flux-pro", "vilva:kling-3"}
    img_spec = next(s for s in specs if s.kind == "image")
    assert img_spec.options["aspect_ratio"] == ("1:1", "16:9", "9:16") and img_spec.base_units == 4
    assert img.media.url == "https://cdn.vilva/i.png" and img.units == 4
    assert vid.media.url == "https://cdn.vilva/v.mp4" and vid.units == 40
    gen_args = dict(state["calls"])["generate_image"]
    assert gen_args["modelId"] == "flux-pro" and gen_args["aspectRatio"] == "16:9"
    assert gen_args["imageUrl"].startswith("data:image/jpeg;base64,")
    assert state["session_ok"]


def test_tool_error():
    async def handler(request):
        msg = await request.json()
        if msg.get("method") == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        if "id" not in msg:
            return web.Response(status=202)
        if msg["method"] == "tools/list":
            return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}})
        return web.json_response({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "isError": True, "content": [{"type": "text", "text": "Not enough credits"}]}})

    app = web.Application()
    app.router.add_post("/mcp", handler)

    async def go(base):
        c = McpClient(base + "/mcp", "k")
        try:
            await c.call_tool("generate_image", {"prompt": "x"})
        except Exception as e:
            return e
        finally:
            await c.close()

    err = asyncio.run(serve(app, go))
    assert err.code == "insufficient_credits"


def _agent_harness(h, base):
    h.app.providers["vilva"] = VilvaProvider("vk_live_x", url=base + "/mcp", poll_interval=0.01)
    h.db.ensure_user(42, "u", 500)


def test_agent_autopilot_charges_actual_and_sends_assets(h):
    app, state = vilva_server([
        {"status": "running"},
        {"status": "completed", "summary": "Готово: 2 файла", "creditsUsed": 10,
         "assets": [{"url": "https://cdn.vilva/a.png"}, {"url": "https://cdn.vilva/b.mp4"}]},
    ])

    async def go(base):
        _agent_harness(h, base)
        await h.afeed(h.msg(BTN_AGENT), h.msg("сделай 2 баннера"), h.cb("am:autopilot"), h.cb("ab:1"))
        await h.drain()
        await h.app.vilva.close()

    asyncio.run(serve(app, go))
    create = dict(state["calls"])["agent_create_run"]
    # бюджет 150 кр бота → 75 кредитов Vilva при наценке 2
    assert create == {"brief": "сделай 2 баннера", "mode": "autopilot", "creditBudget": 75.0}
    # 10 кредитов Vilva * 2 = 20 кр бота
    assert h.db.balance(42) == 500 - 20
    assert any(isinstance(c, SendPhoto) for c in h.session.calls)
    assert any(isinstance(c, SendVideo) for c in h.session.calls)
    assert any("Списано 20 из 150" in t for t in h.session.texts())


def test_agent_plan_first_approve(h):
    app, state = vilva_server([
        {"status": "awaiting_approval"},
        {"status": "running"},
        {"status": "completed", "creditsUsed": 5, "assets": [{"url": "https://cdn.vilva/a.png"}]},
    ])

    async def go(base):
        _agent_harness(h, base)
        await h.afeed(h.msg("/agent карточки для наушников"), h.cb("am:plan_first"), h.cb("ab:0"))
        for _ in range(200):
            if any("План агента" in t for t in h.session.texts()):
                break
            await asyncio.sleep(0.01)
        plan_msg = next(c for c in h.session.calls if isinstance(c, SendMessage) and "План агента" in c.text)
        approve = plan_msg.reply_markup.inline_keyboard[0][0].callback_data
        await h.afeed(h.cb(approve))
        await h.drain()
        await h.app.vilva.close()

    asyncio.run(serve(app, go))
    assert state["approved"] == "approve"
    assert any("5 карточек" in t for t in h.session.texts())
    assert h.db.balance(42) == 500 - 10


def test_agent_cancel_refunds(h):
    app, state = vilva_server([{"status": "running"}])

    async def go(base):
        _agent_harness(h, base)
        await h.afeed(h.msg("/agent видео"), h.cb("am:autopilot"), h.cb("ab:0"))
        start = next(c for c in h.session.calls if isinstance(c, SendMessage) and "Агент запущен" in c.text)
        await h.afeed(h.cb(start.reply_markup.inline_keyboard[0][0].callback_data))
        await h.drain()
        await h.app.vilva.close()

    asyncio.run(serve(app, go))
    assert "agent_cancel_run" in [c[0] for c in state["calls"]]
    assert h.db.balance(42) == 500
    assert any("кредиты вернул" in t for t in h.session.texts())


def test_parse_real_list_models_format():
    """Формат из реального ответа Vilva list_models (key, displayName, kind, credits.perResolution)."""
    from bot.catalog import Catalog
    from bot.config import Config

    data = {"models": [
        {"key": "nano-banana-2", "displayName": "Nano Banana 2", "kind": "image",
         "aspectRatios": ["1:1", "16:9", "auto"], "resolutions": ["1K", "2K", "4K"], "maxReferenceImages": 14,
         "credits": {"base": 12, "perResolution": {"1K": 12, "2K": 18, "4K": 27}}},
        {"key": "nano-banana-2-lite", "displayName": "Nano Banana 2 Lite", "kind": "image",
         "aspectRatios": ["1:1"], "resolutions": ["1K"], "credits": {"base": 4}},
        {"key": "kling", "displayName": "Kling", "kind": "video", "durations": [5, 10],
         "credits": {"base": 50, "perSecond": 10}},
    ]}

    class FakeClient:
        schemas = {}

        async def list_tools(self):
            return {}

        async def call_tool(self, name, values):
            return {"content": [{"type": "text", "text": json.dumps(data)}]}

        async def close(self):
            pass

    specs = asyncio.run(VilvaProvider(client=FakeClient()).discover_models())
    by = {s.key: s for s in specs}
    nb = by["vilva:nano-banana-2"]
    assert nb.title == "Nano Banana 2" and nb.image_field == "image" and nb.simple["aspect_ratio"] == "1:1"
    cat = Catalog(Config(bot_token="x", vilva_credits_per_credit=1, markup=2), specs)
    assert cat.estimate_units(nb, {"resolution": "4K"}) == 27
    assert cat.price(nb, {"resolution": "1K"}) == 24
    kling = by["vilva:kling"]
    assert kling.simple["duration"] == "5" and cat.estimate_units(kling, {"duration": "10"}) == 100
