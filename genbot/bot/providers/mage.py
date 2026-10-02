"""Провайдер Mage (https://docs.mage.space/api/overview).

POST {base}/{architecture}/generate с конфигом модели в теле и заголовком Idempotency-Key,
затем опрос status_url до completed / failed / cancelled. Цена приходит в billing.gems_charged.
"""
from __future__ import annotations

import asyncio
import base64
import random
import uuid

import aiohttp

from .base import GenResult, Media, ProviderError

FINAL = {"completed", "failed", "cancelled"}

USER_MESSAGES = {
    "content_blocked": "Запрос заблокирован фильтром контента. Переформулируй",
    "insufficient_gems": "Сервис временно недоступен, попробуй позже",
    "too_many_requests": "Сейчас очередь, попробуй через минуту",
    "invalid_config": "Эти параметры модель не принимает. Проверь настройки",
}


# Поля, которые Mage ждёт числом, а в настройках бота хранятся строкой.
NUMERIC_FIELDS = {"num_inference_steps", "guidance_scale", "num_frames"}


class MageProvider:
    def __init__(
        self,
        key: str,
        base_url: str = "https://api.mage.space/v1",
        session: aiohttp.ClientSession | None = None,
        first_delay: float | None = None,
        max_delay: float = 15.0,
        timeout: float = 900.0,
    ) -> None:
        if not key:
            raise ValueError("MAGE_API_KEY не задан")
        self._headers = {"Authorization": f"Bearer {key}"}
        self.base_url = base_url.rstrip("/")
        self._session = session
        self.first_delay = first_delay
        self.max_delay = max_delay
        self.timeout = timeout

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def build_config(self, spec, prompt: str, params: dict[str, str], image: bytes | None) -> dict:
        config: dict = {"prompt": prompt}
        if spec.model_id:
            config["model_id"] = spec.model_id
        for k, v in params.items():
            if k == "seed":
                config["seed"] = int(v)
            elif k in NUMERIC_FIELDS:
                num = float(v)
                config[k] = int(num) if num.is_integer() else num
            elif v in ("true", "false"):
                config[k] = v == "true"
            else:
                config[k] = v
        if image is not None:
            if not spec.image_field:
                raise ProviderError("модель не принимает фото", code="invalid_config",
                                    user_message="Эта модель не работает с фото, выбери другую в настройках")
            config[spec.image_field] = "data:image/jpeg;base64," + base64.b64encode(image).decode()
        return config

    async def _submit(self, arch: str, config: dict) -> dict:
        session = await self._get_session()
        headers = {**self._headers, "Idempotency-Key": str(uuid.uuid4())}
        url = f"{self.base_url}/{arch}/generate"
        for attempt in range(3):
            try:
                async with session.post(url, json=config, headers=headers) as r:
                    data = await r.json(content_type=None)
                    if r.status >= 500 and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if r.status >= 400:
                        raise _error(data, r.status)
                    return data
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                # Повтор с тем же Idempotency-Key не спишет gems дважды.
                if attempt == 2:
                    raise ProviderError("mage: нет связи", code="network")
                await asyncio.sleep(2 ** attempt)
        raise ProviderError("mage: не удалось отправить запрос", code="internal_error")

    async def _wait(self, request: dict, kind: str) -> dict:
        session = await self._get_session()
        status_url = request.get("status_url")
        if not status_url:
            raise ProviderError(f"mage: нет status_url в ответе {request}")
        delay = self.first_delay if self.first_delay is not None else (2.0 if kind == "image" else 5.0)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        while request.get("status") not in FINAL:
            if loop.time() > deadline:
                raise ProviderError("mage timeout", code="timeout")
            await asyncio.sleep(delay + random.uniform(0, delay / 4))
            delay = min(delay * 1.5, self.max_delay)
            try:
                async with session.get(status_url, headers=self._headers) as r:
                    data = await r.json(content_type=None)
                    if r.status >= 500:
                        continue
                    if r.status >= 400:
                        raise _error(data, r.status)
                    request = data
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                continue
        return request

    async def generate(self, spec, prompt, params, image) -> GenResult:
        config = self.build_config(spec, prompt, params, image)
        request = await self._submit(spec.arch, config)
        request = await self._wait(request, spec.kind)
        billing = request.get("billing") or {}
        if request["status"] != "completed":
            err = request.get("error") or {}
            code = err.get("code", request["status"])
            raise ProviderError(f"mage {request['status']}: {err}", code=code, user_message=USER_MESSAGES.get(code))
        result = request.get("result") or {}
        url = result.get("url")
        if not url:
            raise ProviderError("mage: готово, но нет url")
        ext = "mp4" if result.get("type", spec.kind) == "video" else "png"
        units = float(billing.get("gems_charged") or 0) - float(billing.get("gems_refunded") or 0)
        return GenResult(Media(url=url, filename=f"{spec.kind}.{ext}"), units or None)


def _error(data, status: int) -> ProviderError:
    err = (data or {}).get("error") or {}
    code = err.get("code", f"http_{status}")
    return ProviderError(f"mage {status} {code}: {err.get('message')}", code=code,
                         user_message=USER_MESSAGES.get(code))
