"""FalProvider против локального фейкового сервера очереди fal."""
from __future__ import annotations

import asyncio

from aiohttp import web

from bot.providers import fal as fal_mod
from bot.providers.base import ProviderError


def make_app(final_status="COMPLETED", result=None):
    state = {"polls": 0, "auth": None, "payload": None}

    async def submit(request):
        state["auth"] = request.headers.get("Authorization")
        state["payload"] = await request.json()
        base = f"http://{request.host}"
        return web.json_response({"status_url": f"{base}/status", "response_url": f"{base}/result"})

    async def status(request):
        state["polls"] += 1
        return web.json_response({"status": "IN_PROGRESS" if state["polls"] < 2 else final_status})

    async def res(request):
        return web.json_response(result or {})

    app = web.Application()
    app.router.add_post("/{model:.*}", submit)
    app.router.add_get("/status", status)
    app.router.add_get("/result", res)
    return app, state


async def _with_server(app, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    old = fal_mod.QUEUE_URL
    fal_mod.QUEUE_URL = f"http://127.0.0.1:{port}"
    provider = fal_mod.FalProvider("k", "img/m", "vid/m", "i2v/m", poll_interval=0.01)
    try:
        return await fn(provider)
    finally:
        fal_mod.QUEUE_URL = old
        await provider.close()
        await runner.cleanup()


def test_image_flow():
    app, state = make_app(result={"images": [{"url": "https://cdn/x.png"}]})
    media = asyncio.run(_with_server(app, lambda p: p.generate_image("кот")))
    assert media.url == "https://cdn/x.png"
    assert state["auth"] == "Key k" and state["payload"]["prompt"] == "кот"


def test_i2v_sends_data_url():
    app, state = make_app(result={"video": {"url": "https://cdn/v.mp4"}})
    media = asyncio.run(_with_server(app, lambda p: p.generate_video("волны", image=b"\xff\xd8")))
    assert media.filename == "video.mp4"
    assert state["payload"]["image_url"].startswith("data:image/jpeg;base64,")


def test_failed_job_raises():
    app, _ = make_app(final_status="FAILED")

    async def call(p):
        try:
            await p.generate_image("x")
        except ProviderError:
            return "raised"

    assert asyncio.run(_with_server(app, call)) == "raised"
