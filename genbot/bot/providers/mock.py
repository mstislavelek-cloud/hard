"""Тестовый провайдер: ничего не тратит, отдаёт заглушку. Для отладки без API-ключей."""
from __future__ import annotations

import base64

from .base import Media, ProviderError

# Однопиксельный PNG.
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class MockProvider:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def generate_image(self, prompt: str) -> Media:
        self.calls.append(("image", prompt))
        if self.fail:
            raise ProviderError("mock failure")
        return Media(data=_PNG, filename="mock.png")

    async def generate_video(self, prompt: str, image: bytes | None = None) -> Media:
        self.calls.append(("video", prompt))
        if self.fail:
            raise ProviderError("mock failure")
        # Видео-заглушку отдаём картинкой: обработчик отправит её как документ.
        return Media(data=_PNG, filename="mock-video.png")
