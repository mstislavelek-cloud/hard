"""Простой фильтр запросов: 18+ и контент с реальными людьми не генерируем."""
from __future__ import annotations

import re

_BLOCKED = (
    r"nsfw", r"nude", r"naked", r"porn", r"sex", r"hentai", r"erotic", r"topless",
    r"undress", r"nipple", r"loli", r"child", r"teen", r"minor",
    r"порн", r"голая", r"голый", r"обнаж", r"эрот", r"секс", r"хентай", r"раздень",
    r"раздет", r"интим", r"школьниц", r"несовершеннолет", r"ребён", r"ребен", r"малолет",
)
_PATTERN = re.compile("|".join(_BLOCKED), re.IGNORECASE)

MAX_PROMPT_LEN = 1000


def check_prompt(prompt: str) -> str | None:
    """Возвращает текст причины отказа или None, если запрос допустим."""
    text = prompt.strip()
    if not text:
        return "Опиши, что сгенерировать: например, «кроссовки на белом фоне, студийный свет»"
    if len(text) > MAX_PROMPT_LEN:
        return f"Слишком длинный запрос, максимум {MAX_PROMPT_LEN} символов"
    if _PATTERN.search(text):
        return "Такой контент бот не генерирует"
    return None
