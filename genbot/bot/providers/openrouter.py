"""Картинки через OpenRouter (модели с выводом изображений, например Gemini Flash Image).

OpenRouter принимает оплату криптовалютой, поэтому удобен для пополнения из РФ.
"""
from __future__ import annotations

import base64

import aiohttp

from .base import Media, ProviderError

API_URL = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterImageProvider:
    def __init__(self, key: str, model: str, session: aiohttp.ClientSession | None = None) -> None:
        if not key:
            raise ValueError("OPENROUTER_API_KEY не задан")
        self._headers = {"Authorization": f"Bearer {key}"}
        self.model = model
        self._session = session

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def generate_image(self, prompt: str) -> Media:
        session = await self._get_session()
        payload = {
            "model": self.model,
            "modalities": ["image", "text"],
            "messages": [{"role": "user", "content": prompt}],
        }
        async with session.post(API_URL, json=payload, headers=self._headers) as r:
            if r.status >= 400:
                raise ProviderError(f"openrouter {r.status}: {await r.text()}")
            data = await r.json()
        try:
            url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderError("openrouter: нет картинки в ответе") from e
        return parse_image_url(url)


def parse_image_url(url: str) -> Media:
    """OpenRouter обычно отдаёт data:-URL в base64; обычный URL пропускаем как есть."""
    if url.startswith("data:"):
        try:
            _, b64 = url.split(",", 1)
            return Media(data=base64.b64decode(b64), filename="image.png")
        except ValueError as e:
            raise ProviderError("openrouter: битый data URL") from e
    return Media(url=url, filename="image.png")
