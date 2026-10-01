"""MageProvider против локального фейкового API Mage."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from bot.catalog import MAGE_MODELS
from bot.providers.base import ProviderError
from bot.providers.mage import MageProvider
from tests.conftest import serve

LEMON = next(m for m in MAGE_MODELS if m.key == "mage:lemon")
FLARE = next(m for m in MAGE_MODELS if m.key == "mage:gpt-image-2.5-flare")


def mage_app(final="completed", submit_status=202, submit_error=None, error=None, result_type="video"):
    state = {"polls": 0, "body": None, "headers": None, "path": None}

    async def submit(request):
        state["path"] = request.path
        state["headers"] = dict(request.headers)
        state["body"] = await request.json()
        if submit_error:
            return web.json_response({"error": submit_error}, status=submit_status)
        base = f"http://{request.host}"
        return web.json_response({
            "request_id": "r1", "status": "in_progress", "billing": {"mode": "gems", "gems_charged": 400},
            "result": None, "error": None, "status_url": f"{base}/v1/requests/r1/status",
        })

    async def status(request):
        state["polls"] += 1
        if state["polls"] < 2:
            return web.json_response({"request_id": "r1", "status": "in_progress", "status_url": str(request.url)})
        body = {"request_id": "r1", "status": final, "status_url": str(request.url),
                "billing": {"mode": "gems", "gems_charged": 400}}
        if final == "completed":
            body["result"] = {"type": result_type, "url": "https://cdn.mage/out", "seed": 5}
        else:
            body["error"] = error or {"code": "generation_failed", "message": "x"}
            body["billing"]["gems_refunded"] = 400
        return web.json_response(body)

    app = web.Application()
    app.router.add_post("/v1/{arch}/generate", submit)
    app.router.add_get("/v1/requests/r1/status", status)
    return app, state


def run(app, spec, params, image=None):
    async def go(base):
        p = MageProvider("mage_sk_x", base_url=base + "/v1", first_delay=0.01, max_delay=0.02)
        try:
            return await p.generate(spec, "волны", params, image)
        finally:
            await p.close()

    return asyncio.run(serve(app, go))


def test_video_request_shape_and_result():
    app, state = mage_app()
    res = run(app, LEMON, {"duration": "5", "aspect_ratio": "9:16", "audio": "false", "seed": "42"}, image=b"\xff")
    assert state["path"] == "/v1/lemon/generate"
    assert state["headers"]["Authorization"] == "Bearer mage_sk_x"
    assert state["headers"]["Idempotency-Key"]
    body = state["body"]
    assert body["model_id"] == "lemon" and body["duration"] == "5" and body["audio"] is False and body["seed"] == 42
    assert body["first_image"].startswith("data:image/jpeg;base64,")
    assert res.media.url == "https://cdn.mage/out" and res.media.filename == "video.mp4" and res.units == 400


def test_image_uses_reference_field():
    app, state = mage_app(result_type="image")
    res = run(app, FLARE, {"aspect_ratio": "1:1"}, image=b"\xff")
    assert state["path"] == "/v1/gpt_image_2/generate" and "image" in state["body"]
    assert res.media.filename == "image.png"


def test_failed_content_blocked():
    app, _ = mage_app(final="failed", error={"code": "content_blocked", "message": "no"})
    with pytest.raises(ProviderError) as e:
        run(app, FLARE, {})
    assert e.value.code == "content_blocked" and "фильтром" in e.value.user_message


def test_submit_error_envelope():
    app, _ = mage_app(submit_status=402, submit_error={"code": "insufficient_gems", "message": "low"})
    with pytest.raises(ProviderError) as e:
        run(app, FLARE, {})
    assert e.value.code == "insufficient_gems"
