"""Общий интерфейс провайдеров генерации."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProviderError(Exception):
    """Генерация не удалась; кредиты нужно вернуть."""


@dataclass
class Media:
    """Результат генерации: либо URL, либо байты."""

    url: str | None = None
    data: bytes | None = None
    filename: str = "result"


class ImageProvider(Protocol):
    async def generate_image(self, prompt: str) -> Media: ...


class VideoProvider(Protocol):
    async def generate_video(self, prompt: str, image: bytes | None = None) -> Media: ...
