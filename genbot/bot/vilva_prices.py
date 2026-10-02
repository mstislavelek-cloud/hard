"""Цены Vilva из их документации и интерфейса (2026-10), в кредитах Vilva.

Используются, когда list_models не отдаёт подробную цену (по разрешению, длительности, effort).
Модели сопоставляются по названию без учёта регистра и пробелов.
"""
from __future__ import annotations

import dataclasses
import re

# Картинки: фиксированная цена или сетка по разрешению / (разрешение, effort).
IMAGE_PRICES: dict[str, dict] = {
    "nano banana": {"flat": 6},
    "nano banana 2": {"resolution": {"1K": 12, "2K": 18, "4K": 27}},
    "nano banana 2 lite": {"flat": 6},
    "nano banana pro": {"resolution": {"1K": 27, "2K": 27, "4K": 36}},
    "seedream 5 lite": {"resolution": {"2K": 9, "3K": 9}},
    "seedream 5 pro": {"resolution": {"1K": 11, "2K": 21}},
    "seedream 5 flash": {"flat": 6},
    "flux 2 pro": {"resolution": {"1K": 8, "2K": 11}},
    "flux 2 flex": {"resolution": {"1K": 21, "2K": 36}},
    "flux 2 lora": {"flat": 13},
    "grok imagine": {"flat": 6},
    "gpt-4o image": {"flat": 9},
    "z image": {"flat": 2},
    "midjourney": {"flat": 30},
    "qwen image 3": {"resolution": {"1K": 10, "2K": 10}},
    "qwen image 3 pro": {"resolution": {"1K": 12, "2K": 21}},
    "qwen image 2.1": {"resolution": {"1K": 6, "2K": 12}},
    "gpt image 2.5 flare": {"resolution": {"1K": 9, "2K": 15, "4K": 24}},
    "gpt image 2.5 sunburst": {"resolution": {"1K": 9, "2K": 15, "4K": 24}},
    "gpt image 2": {"grid": ("resolution", "effort"), "values": {
        ("1K", "auto"): 9, ("1K", "low"): 5, ("1K", "medium"): 17, ("1K", "high"): 66,
        ("2K", "auto"): 15, ("2K", "low"): 18, ("2K", "medium"): 68, ("2K", "high"): 266,
    }},
}

# Видео: кредиты в секунду (диапазон от младшего разрешения к старшему) или за ролик.
VIDEO_PRICES: dict[str, dict] = {
    "seedance 2.0": {"per_second": (18, 153), "resolutions": ("480p", "720p", "1080p")},
    "seedance 2.0 fast": {"per_second": (14, 50), "resolutions": ("480p", "720p")},
    "grok imagine": {"per_second": (15, 38), "resolutions": ("480p", "720p", "1080p")},
    "kling 3.0": {"per_second": (21, 41)},
    "kling 2.6 motion": {"per_second": (9, 14)},
    "kling 3.0 motion": {"per_second": (30, 41)},
    "kling ai avatar": {"per_second": (12, 24)},
    "kling ai avatar v2": {"per_second": (17, 35)},
    "veo 3.1 fast": {"per_video": (90, 270)},
    "veo 3.1 quality": {"per_video": (375, 555)},
}

# Мегапиксели кадра — для оценки цены промежуточных разрешений внутри известного диапазона.
_MEGAPIXELS = {"360p": 0.23, "480p": 0.41, "540p": 0.52, "720p": 0.92, "1080p": 2.07, "1440p": 3.69,
               "2k": 3.69, "4k": 8.29}


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", title.lower().replace("_", " ")).strip()


def _spread(lo: float, hi: float, resolutions: tuple[str, ...]) -> dict[str, float]:
    """Цена по разрешениям внутри диапазона, пропорционально площади кадра."""
    if len(resolutions) == 1:
        return {resolutions[0]: float(hi)}
    mps = [_MEGAPIXELS.get(r.lower(), 0.0) for r in resolutions]
    if not all(mps) or mps[0] == mps[-1]:
        # Неизвестные разрешения — берём верх диапазона, чтобы не уйти в минус.
        return {r: float(hi) for r in resolutions}
    return {r: round(lo + (mp - mps[0]) / (mps[-1] - mps[0]) * (hi - lo), 2) for r, mp in zip(resolutions, mps)}


def apply_doc_prices(spec):
    """Дополняет модель Vilva ценами из документации, если list_models не дал подробной цены."""
    name = _norm(spec.title)
    if spec.kind == "image":
        info = IMAGE_PRICES.get(name)
        if not info:
            return spec
        if "flat" in info:
            if spec.price_table or spec.price_grid:
                return spec
            return dataclasses.replace(spec, base_units=float(info["flat"]))
        if "resolution" in info:
            table = info["resolution"]
            options = dict(spec.options)
            options.setdefault("resolution", tuple(table))
            simple = dict(spec.simple)
            simple.setdefault("resolution", options["resolution"][0])
            return dataclasses.replace(spec, base_units=float(min(table.values())), options=options, simple=simple,
                                       price_table={"resolution": {k: float(v) for k, v in table.items()}})
        if "grid" in info:
            keys, values = info["grid"], info["values"]
            options = dict(spec.options)
            for i, k in enumerate(keys):
                options.setdefault(k, tuple(dict.fromkeys(v[i] for v in values)))
            simple = {**spec.simple, **{k: options[k][0] for k in keys if k not in spec.simple}}
            return dataclasses.replace(spec, base_units=float(min(values.values())), options=options, simple=simple,
                                       price_keys=tuple(keys), price_grid={k: float(v) for k, v in values.items()},
                                       price_table={})
        return spec

    info = VIDEO_PRICES.get(name)
    if not info or spec.rate_by_res or spec.per_second:
        return spec
    resolutions = spec.options.get("resolution") or info.get("resolutions") or ()
    if "per_second" in info:
        lo, hi = info["per_second"]
        if resolutions:
            rates = _spread(lo, hi, tuple(resolutions))
            options = dict(spec.options)
            options.setdefault("resolution", tuple(resolutions))
            simple = dict(spec.simple)
            simple.setdefault("resolution", options["resolution"][0])
            return dataclasses.replace(spec, rate_by_res=rates, options=options, simple=simple,
                                       base_units=round(rates[options["resolution"][0]] * 5))
        # Отчего зависит диапазон, неизвестно — считаем по верхней границе.
        return dataclasses.replace(spec, per_second=float(hi), base_units=float(hi) * 5)
    lo, hi = info["per_video"]
    if resolutions:
        table = _spread(lo, hi, tuple(resolutions))
        return dataclasses.replace(spec, base_units=float(lo), price_table={"resolution": table})
    return dataclasses.replace(spec, base_units=float(hi))
