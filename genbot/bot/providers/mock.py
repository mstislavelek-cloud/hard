"""Тестовый провайдер: ничего не тратит, отдаёт заглушку. Для отладки без API-ключей."""
from __future__ import annotations

import base64

from .base import GenResult, Media, ProviderError

# Однопиксельный PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class MockProvider:
    def __init__(self, fail: bool = False, units: float | None = None) -> None:
        self.fail = fail
        self.units = units
        self.calls: list[tuple[str, str, dict, bool]] = []

    async def generate(self, spec, prompt, params, image):
        self.calls.append((spec.key, prompt, dict(params), image is not None))
        if self.fail:
            raise ProviderError("mock failure", code="generation_failed")
        name = "mock.png" if spec.kind == "image" else "mock-video.png"
        return GenResult(Media(data=PNG, filename=name), self.units if self.units is not None else spec.base_units)
