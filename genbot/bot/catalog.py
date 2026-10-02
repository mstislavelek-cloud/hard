"""Каталог моделей: что можно выбрать в боте, параметры и оценка цены в кредитах."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Config

IMAGE_ASPECTS = ("1:1", "4:5", "2:3", "9:16", "16:9", "3:2", "5:4", "21:9", "9:21")
# Запасная оценка, если у модели нет точной сетки цен.
RES_SCALE = {"1K": 1.0, "2K": 2.0, "3K": 3.0, "4K": 4.0,
             "480p": 1.0, "720p": 2.25, "1080p": 4.0, "4k": 9.0}


@dataclass(frozen=True)
class ModelSpec:
    key: str                     # "mage:lemon", "vilva:<id>", "mock:image"
    provider: str                # mage | vilva | mock
    kind: str                    # image | video
    title: str
    arch: str                    # архитектура Mage или id модели Vilva
    model_id: str | None = None  # вариант модели Mage
    base_units: float = 0.0      # цена у провайдера при базовых параметрах (gems / кредиты Vilva)
    options: dict[str, tuple[str, ...]] = field(default_factory=dict)
    base: dict[str, str] = field(default_factory=dict)      # параметры, при которых известна base_units
    simple: dict[str, str] = field(default_factory=dict)    # параметры простого режима
    image_field: str | None = None  # куда класть фото пользователя
    note: str = ""
    # Точные цены по значению параметра, например {"resolution": {"1K": 12, "2K": 18}}.
    price_table: dict[str, dict[str, float]] = field(default_factory=dict)
    # Цена за секунду видео (если провайдер считает так).
    per_second: float = 0.0
    # Точная сетка цен: ключ — значения параметров price_keys по порядку, например ("1K", "high") → 317.
    price_keys: tuple[str, ...] = ()
    price_grid: dict[tuple, float] = field(default_factory=dict)
    # Цена за секунду по разрешению: {"480p": 81.6, "720p": 168}.
    rate_by_res: dict[str, float] = field(default_factory=dict)


def _mage_image(model_id: str, arch: str, title: str, grid: dict[tuple, float], note: str,
                quality: tuple[str, ...] = ()) -> ModelSpec:
    res = tuple(dict.fromkeys(k[0] for k in grid))
    options = {"aspect_ratio": IMAGE_ASPECTS, "resolution": res}
    keys: tuple[str, ...] = ("resolution",)
    simple = {"aspect_ratio": "1:1", "resolution": res[0]}
    if quality:
        options["quality"] = quality
        keys = ("resolution", "quality")
        simple["quality"] = quality[0]
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="image", title=title, arch=arch, model_id=model_id,
        base_units=min(grid.values()), options=options, simple=simple, image_field="image", note=note,
        price_keys=keys, price_grid=grid,
    )


def _mage_video(model_id: str, arch: str, title: str, rates: dict[str, float], aspects, durations,
                image_field: str, note: str, extra: dict | None = None) -> ModelSpec:
    options = {"aspect_ratio": aspects, "resolution": tuple(rates), "duration": durations}
    if extra:
        options.update(extra)
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="video", title=title, arch=arch, model_id=model_id,
        base_units=math.ceil(rates["480p"] * 5 - 1e-9), options=options,
        simple={"aspect_ratio": "9:16", "resolution": "480p", "duration": "5"},
        image_field=image_field, note=note, rate_by_res=rates,
    )


# Цены сняты через estimate_cost Mage 2026-10-02 (gems). Картинки — за штуку, видео — за секунду.
# Формат кадра, звук и голоса на цену не влияют.
MAGE_MODELS: tuple[ModelSpec, ...] = (
    _mage_image("gpt-image-2.5-flare", "gpt_image_2", "GPT Image 2.5 Flare",
                {("1K", "low"): 9, ("1K", "medium"): 80, ("1K", "high"): 317,
                 ("2K", "low"): 18, ("2K", "medium"): 160, ("2K", "high"): 634},
                "точно следует промпту, текст на картинке; quality сильно влияет на цену",
                quality=("low", "medium", "high")),
    _mage_image("guava-2", "guava", "Guava 2", {("1K",): 36, ("2K",): 36}, "фотореализм, быстрее"),
    _mage_image("guava-2-pro", "guava", "Guava 2 Pro", {("1K",): 48, ("2K",): 90}, "фотореализм: портреты, товары"),
    _mage_image("mango-v3s", "mango", "Mango 3S", {("2K",): 55, ("3K",): 55}, "персонажи и референсы, до 3K"),
    _mage_image("mango-v3", "mango", "Mango 3", {("1K",): 68, ("2K",): 135}, "флагман Mage, точные правки"),
    _mage_image("mango-v2", "mango", "Mango 2", {("2K",): 60, ("3K",): 60, ("4K",): 60}, "единственная до 4K"),
    _mage_video("lemon", "lemon", "Lemon", {"480p": 81.6, "720p": 168, "1080p": 336},
                ("9:16", "16:9", "1:1", "3:4", "4:3"), ("3", "5", "8", "10", "15", "20", "30"), "first_image",
                "баланс цены и качества, со звуком, оживляет фото", {"audio": ("true", "false")}),
    _mage_video("cherry-mini", "cherry", "Cherry Mini", {"480p": 60, "720p": 120},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15"), "image", "самое дешёвое видео"),
    _mage_video("cherry", "cherry", "Cherry", {"480p": 90, "720p": 180},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15"), "image", "кино-движение дешевле флагмана"),
    _mage_video("cherry-pro", "cherry", "Cherry Pro", {"480p": 105, "720p": 225, "1080p": 555, "4k": 1170},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15"), "image", "единственная до 4K"),
    _mage_video("cherry-2-pro", "cherry", "Cherry 2 Pro", {"480p": 154.5, "720p": 346.5, "1080p": 853.5},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15", "20", "30"), "image",
                "флагман, до 30 сек"),
)

MOCK_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("mock:image", "mock", "image", "Тестовая картинка", "mock", base_units=6,
              options={"aspect_ratio": ("1:1", "9:16")}, simple={"aspect_ratio": "1:1"}, image_field="image"),
    ModelSpec("mock:video", "mock", "video", "Тестовое видео", "mock", base_units=60,
              options={"duration": ("5", "10")}, base={"duration": "5"}, simple={"duration": "5"},
              image_field="image"),
)


class Catalog:
    def __init__(self, cfg: Config, models: list[ModelSpec]) -> None:
        self.cfg = cfg
        self.models = list(models)

    def by_kind(self, kind: str) -> list[ModelSpec]:
        return [m for m in self.models if m.kind == kind]

    def get(self, key: str | None) -> ModelSpec | None:
        return next((m for m in self.models if m.key == key), None)

    def default(self, kind: str) -> ModelSpec | None:
        want = self.cfg.default_image_model if kind == "image" else self.cfg.default_video_model
        return self.get(want) or next(iter(self.by_kind(kind)), None)

    def add(self, models: list[ModelSpec]) -> None:
        known = {m.key for m in self.models}
        self.models.extend(m for m in models if m.key not in known)

    # --- цены ---

    def estimate_units(self, spec: ModelSpec, params: dict[str, str]) -> float:
        """Стоимость у провайдера (gems / кредиты Vilva) при выбранных параметрах."""
        p = {**spec.simple, **params}
        if spec.rate_by_res:
            rate = spec.rate_by_res.get(p.get("resolution", ""), next(iter(spec.rate_by_res.values())))
            try:
                return float(math.ceil(rate * float(p.get("duration", 5)) - 1e-9))
            except ValueError:
                return float(spec.base_units)
        if spec.price_grid:
            key = tuple(p.get(k) for k in spec.price_keys)
            if key in spec.price_grid:
                return float(spec.price_grid[key])
            return float(max(spec.price_grid.values()))  # неизвестная комбинация — берём дорогую, не в минус
        units = spec.base_units
        for name, table in spec.price_table.items():
            if params.get(name) in table:
                units = table[params[name]]
        if spec.per_second and params.get("duration"):
            try:
                scale = units / spec.base_units if spec.base_units else 1.0
                return spec.per_second * float(params["duration"]) * scale
            except ValueError:
                return units
        if spec.price_table:
            return units
        if "duration" in params and "duration" in spec.base:
            try:
                units *= float(params["duration"]) / float(spec.base["duration"])
            except ValueError:
                pass
        if "resolution" in params and "resolution" in spec.base:
            units *= RES_SCALE.get(params["resolution"], 1.0) / RES_SCALE.get(spec.base["resolution"], 1.0)
        return units

    def credits(self, spec: ModelSpec, units: float) -> int:
        """Перевод стоимости у провайдера в кредиты бота с наценкой."""
        return max(1, math.ceil(self.usd(spec.provider, units) * self.cfg.markup / self.cfg.credit_usd - 1e-9))

    def usd(self, provider: str, units: float) -> float:
        """Себестоимость в $ по ценам провайдера (mock считаем как Mage)."""
        rate = self.cfg.vilva_usd_per_credit if provider == "vilva" else self.cfg.mage_usd_per_gem
        return units * rate

    def price(self, spec: ModelSpec, params: dict[str, str]) -> int:
        return self.credits(spec, self.estimate_units(spec, params))


def build_catalog(cfg: Config) -> Catalog:
    models: list[ModelSpec] = []
    if cfg.mage_key:
        models.extend(MAGE_MODELS)
    if cfg.use_mock:
        models.extend(MOCK_MODELS)
    return Catalog(cfg, models)
