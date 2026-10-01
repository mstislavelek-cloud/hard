"""Общие помощники для разбора ответов провайдеров."""
from __future__ import annotations

import json
import re
from typing import Any

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def find_first_url(obj: Any, prefer: tuple[str, ...] = ()) -> str | None:
    """Ищет первый http(s)-URL в JSON-подобной структуре или тексте.

    Если задан prefer (например, (".mp4",)), сначала возвращает URL с таким окончанием.
    """
    urls = list(_iter_urls(obj))
    for suffix in prefer:
        for u in urls:
            if u.split("?", 1)[0].lower().endswith(suffix):
                return u
    return urls[0] if urls else None


def _iter_urls(obj: Any):
    if isinstance(obj, str):
        # Текст может оказаться JSON-строкой.
        stripped = obj.strip()
        if stripped[:1] in "{[":
            try:
                yield from _iter_urls(json.loads(stripped))
                return
            except ValueError:
                pass
        yield from _URL.findall(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_urls(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_urls(v)


def find_key(obj: Any, *names: str) -> Any:
    """Рекурсивно ищет значение первого найденного ключа из names (в том числе в JSON-строках)."""
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped[:1] in "{[":
            try:
                return find_key(json.loads(stripped), *names)
            except ValueError:
                return None
        return None
    if isinstance(obj, dict):
        for n in names:
            if n in obj and obj[n] not in (None, ""):
                return obj[n]
        for v in obj.values():
            found = find_key(v, *names)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_key(v, *names)
            if found is not None:
                return found
    return None
