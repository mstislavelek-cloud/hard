"""Провайдер Mage (api.mage.space): картинки и видео.

Поля config взяты из описания моделей Mage (prompt, model_id, aspect_ratio, resolution,
duration, image). Пути REST-эндпоинтов вынесены в настройки: сверь их с
https://docs.mage.space/api/overview перед запуском.
"""
from __future__ import annotations

import asyncio
import base64
import uuid

import aiohttp

from .base import Media, ProviderError
from .util import find_first_url, find_key

DONE = {"completed", "succeeded", "success", "done"}
FAILED = {"failed", "error", "cancelled", "canceled"}


class MageProvider:
    def __init__(
        self,
        key: str,
        base_url: str = "https://api.mage.space",
        submit_path: str = "/v1/generate",
        status_path: str = "/v1/requests/{id}",
        image_arch: str = "gpt_image_2",
        image_model: str = "gpt-image-2.5-flare",
        image_extra: dict | None = None,
        video_arch: str = "cherry",
        video_model: str = "cherry-mini",
        video_extra: dict | None = None,
        session: aiohttp.ClientSession | None = None,
        poll_interval: float = 3.0,
        timeout: float = 600.0,
    ) -> None:
        if not key:
            raise ValueError("MAGE_API_KEY не задан")
        self._headers = {"Authorization": f"Bearer {key}"}
        self.base_url = base_url.rstrip("/")
        self.submit_path = submit_path
        self.status_path = status_path
        self.image_arch, self.image_model = image_arch, image_model
        self.image_extra = image_extra or {"resolution": "1K", "aspect_ratio": "1:1"}
        self.video_arch, self.video_model = video_arch, video_model
        self.video_extra = video_extra or {"resolution": "480p", "duration": "5", "aspect_ratio": "9:16"}
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

    async def _run(self, arch: str, config: dict, prefer: tuple[str, ...]) -> str:
        session = await self._get_session()
        body = {"architecture": arch, "config": config, "idempotency_key": str(uuid.uuid4())}
        async with session.post(self.base_url + self.submit_path, json=body, headers=self._headers) as r:
            if r.status >= 400:
                raise ProviderError(f"mage submit {r.status}: {await r.text()}")
            job = await r.json()
        url = _result_url(job, prefer)
        if url:
            return url
        req_id = find_key(job, "request_id", "id")
        if not req_id:
            raise ProviderError(f"mage: нет id запроса в ответе {job}")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        status_url = self.base_url + self.status_path.format(id=req_id)
        while True:
            async with session.get(status_url, headers=self._headers) as r:
                if r.status >= 400:
                    raise ProviderError(f"mage status {r.status}: {await r.text()}")
                data = await r.json()
            status = str(find_key(data, "status") or "").lower()
            if status in FAILED:
                raise ProviderError(f"mage: {status} {find_key(data, 'error', 'message')}")
            if status in DONE:
                url = _result_url(data, prefer)
                if not url:
                    raise ProviderError("mage: готово, но нет url результата")
                return url
            if loop.time() > deadline:
                raise ProviderError("mage timeout")
            await asyncio.sleep(self.poll_interval)

    async def generate_image(self, prompt: str) -> Media:
        config = {"prompt": prompt, "model_id": self.image_model, **self.image_extra}
        url = await self._run(self.image_arch, config, (".png", ".jpg", ".jpeg", ".webp"))
        return Media(url=url, filename="image.png")

    async def generate_video(self, prompt: str, image: bytes | None = None) -> Media:
        config = {"prompt": prompt, "model_id": self.video_model, **self.video_extra}
        if image is not None:
            config["image"] = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        url = await self._run(self.video_arch, config, (".mp4", ".webm", ".mov"))
        return Media(url=url, filename="video.mp4")


def _result_url(data: dict, prefer: tuple[str, ...]) -> str | None:
    result = find_key(data, "result", "output", "outputs")
    return find_first_url(result, prefer) if result is not None else None
