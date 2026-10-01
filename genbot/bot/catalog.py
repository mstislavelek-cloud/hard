"""Каталог моделей: что можно выбрать в боте, параметры и оценка цены в кредитах."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Config

IMAGE_ASPECTS = ("1:1", "4:5", "2:3", "9:16", "16:9", "3:2", "5:4", "21:9", "9:21")
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


def _mage_image(model_id: str, arch: str, title: str, gems: float, res: tuple[str, ...], note: str) -> ModelSpec:
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="image", title=title, arch=arch, model_id=model_id,
        base_units=gems, options={"aspect_ratio": IMAGE_ASPECTS, "resolution": res},
        base={"resolution": res[0]}, simple={"aspect_ratio": "1:1", "resolution": res[0]},
        image_field="image", note=note,
    )


def _mage_video(model_id: str, arch: str, title: str, gems: float, aspects, res, durations,
                base_duration: str, image_field: str, note: str, extra: dict | None = None) -> ModelSpec:
    options = {"aspect_ratio": aspects, "resolution": res, "duration": durations}
    if extra:
        options.update(extra)
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="video", title=title, arch=arch, model_id=model_id,
        base_units=gems, options=options, base={"resolution": "480p", "duration": base_duration},
        simple={"aspect_ratio": "9:16", "resolution": "480p", "duration": "5"},
        image_field=image_field, note=note,
    )


# Цены в gems при базовых параметрах взяты из каталога Mage (list_models), 2026-10.
MAGE_MODELS: tuple[ModelSpec, ...] = (
    _mage_image("gpt-image-2.5-flare", "gpt_image_2", "GPT Image 2.5 Flare", 9, ("1K", "2K"),
                "дёшево, точно следует промпту, умеет текст на картинке"),
    _mage_image("guava-2", "guava", "Guava 2", 36, ("1K", "2K"), "фотореализм, быстрее"),
    _mage_image("guava-2-pro", "guava", "Guava 2 Pro", 48, ("1K", "2K"), "фотореализм: портреты, товары"),
    _mage_image("mango-v3s", "mango", "Mango 3S", 55, ("2K", "3K"), "персонажи и референсы, до 3K"),
    _mage_image("mango-v3", "mango", "Mango 3", 135, ("1K", "2K"), "флагман Mage, точные правки"),
    _mage_image("mango-v2", "mango", "Mango 2", 60, ("2K", "3K", "4K"), "единственная до 4K"),
    _mage_video("lemon", "lemon", "Lemon", 245, ("9:16", "16:9", "1:1", "3:4", "4:3"),
                ("480p", "720p", "1080p"), ("3", "5", "8", "10", "15", "20", "30"), "3", "first_image",
                "баланс цены и качества, со звуком, оживляет фото", {"audio": ("true", "false")}),
    _mage_video("cherry-mini", "cherry", "Cherry Mini", 240, ("9:16", "16:9", "1:1"),
                ("480p", "720p"), ("4", "5", "8", "10", "15"), "4", "image", "самое дешёвое видео"),
    _mage_video("cherry", "cherry", "Cherry", 360, ("9:16", "16:9", "1:1"),
                ("480p", "720p"), ("4", "5", "8", "10", "15"), "4", "image", "кино-движение дешевле флагмана"),
    _mage_video("cherry-2-pro", "cherry", "Cherry 2 Pro", 618, ("9:16", "16:9", "1:1"),
                ("480p", "720p", "1080p"), ("4", "5", "8", "10", "15", "20", "30"), "4", "image",
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
        """Оценка стоимости у провайдера с учётом длительности и разрешения."""
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
        if spec.provider == "vilva":
            per_credit = self.cfg.vilva_credits_per_credit
        else:
            per_credit = self.cfg.gems_per_credit
        return max(1, math.ceil(units * self.cfg.markup / per_credit))

    def price(self, spec: ModelSpec, params: dict[str, str]) -> int:
        return self.credits(spec, self.estimate_units(spec, params))


def build_catalog(cfg: Config) -> Catalog:
    models: list[ModelSpec] = []
    if cfg.mage_key:
        models.extend(MAGE_MODELS)
    if cfg.use_mock:
        models.extend(MOCK_MODELS)
    return Catalog(cfg, models)
