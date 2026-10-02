"""Общий интерфейс провайдеров генерации."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..catalog import ModelSpec


class ProviderError(Exception):
    """Генерация не удалась; кредиты нужно вернуть.

    code — машинный код причины (например, content_blocked), user_message — что сказать пользователю.
    """

    def __init__(self, message: str, code: str = "error", user_message: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = user_message


@dataclass
class Media:
    """Результат генерации: либо URL, либо байты."""

    url: str | None = None
    data: bytes | None = None
    filename: str = "result"


@dataclass
class GenResult:
    media: Media
    # Фактическая стоимость у провайдера (gems / кредиты Vilva), если известна.
    units: float | None = None


class Provider(Protocol):
    async def generate(
        self, spec: "ModelSpec", prompt: str, params: dict[str, str], image: bytes | None
    ) -> GenResult: ...
