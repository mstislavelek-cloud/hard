"""Провайдер fal.ai через очередь (queue.fal.run): картинки и видео."""
from __future__ import annotations

import asyncio
import base64

import aiohttp

from .base import Media, ProviderError

QUEUE_URL = "https://queue.fal.run"


class FalProvider:
    def __init__(
        self,
        key: str,
        image_model: str,
        video_model: str,
        i2v_model: str,
        session: aiohttp.ClientSession | None = None,
        poll_interval: float = 2.0,
        timeout: float = 600.0,
    ) -> None:
        if not key:
            raise ValueError("FAL_KEY не задан")
        self._headers = {"Authorization": f"Key {key}"}
        self.image_model = image_model
        self.video_model = video_model
        self.i2v_model = i2v_model
        self._session = session
        self.poll_interval = poll_interval
        self.timeout = timeout

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _run(self, model: str, payload: dict) -> dict:
        session = await self._get_session()
        async with session.post(f"{QUEUE_URL}/{model}", json=payload, headers=self._headers) as r:
            if r.status >= 400:
                raise ProviderError(f"fal submit {r.status}: {await r.text()}")
            job = await r.json()
        status_url, response_url = job["status_url"], job["response_url"]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while True:
            async with session.get(status_url, headers=self._headers) as r:
                if r.status >= 400:
                    raise ProviderError(f"fal status {r.status}: {await r.text()}")
                status = (await r.json()).get("status")
            if status == "COMPLETED":
                break
            if status not in ("IN_QUEUE", "IN_PROGRESS"):
                raise ProviderError(f"fal job status {status}")
            if loop.time() > deadline:
                raise ProviderError("fal timeout")
            await asyncio.sleep(self.poll_interval)
        async with session.get(response_url, headers=self._headers) as r:
            if r.status >= 400:
                raise ProviderError(f"fal result {r.status}: {await r.text()}")
            return await r.json()

    async def generate_image(self, prompt: str) -> Media:
        result = await self._run(
            self.image_model, {"prompt": prompt, "num_images": 1, "enable_safety_checker": True}
        )
        if result.get("has_nsfw_concepts") and any(result["has_nsfw_concepts"]):
            raise ProviderError("nsfw filtered")
        images = result.get("images") or []
        if not images:
            raise ProviderError("fal: пустой ответ")
        return Media(url=images[0]["url"], filename="image.png")

    async def generate_video(self, prompt: str, image: bytes | None = None) -> Media:
        payload: dict = {"prompt": prompt, "duration": "5"}
        model = self.video_model
        if image is not None:
            payload["image_url"] = "data:image/jpeg;base64," + base64.b64encode(image).decode()
            model = self.i2v_model
        result = await self._run(model, payload)
        video = result.get("video") or {}
        if not video.get("url"):
            raise ProviderError("fal: нет видео в ответе")
        return Media(url=video["url"], filename="video.mp4")
