"""Каталог моделей: что можно выбрать в боте, параметры и оценка цены в кредитах."""
from __future__ import annotations

import dataclasses
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
    family: str = ""             # семейство для меню выбора (GPT Image, Seedance, Nano Banana…)
    # Доплата за фото-референс: + image_surcharge единиц, затем × image_multiplier.
    image_surcharge: float = 0.0
    image_multiplier: float = 1.0
    # Как значение параметра меняет цену: {"web_search": {"true": (20, 1.0)}} — (+gems, ×множитель).
    param_prices: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    # Параметры со свободным текстом (негативный промпт), вводятся сообщением.
    text_params: tuple[str, ...] = ()


def _durations(available: tuple[str, ...]) -> tuple[str, ...]:
    """Короткий список длительностей для кнопок из того, что поддерживает модель."""
    nice = ("3", "4", "5", "6", "8", "10", "12", "15", "16", "20", "25", "30")
    picked = tuple(d for d in nice if d in available)
    return picked or available


def _mage_image(model_id: str, arch: str, title: str, grid, note: str, quality: tuple[str, ...] = (),
                aspects: tuple[str, ...] = IMAGE_ASPECTS, image_field: str | None = "image",
                family: str = "", key: str = "") -> ModelSpec:
    """grid: число (одна цена) или {разрешение: gems} или {(разрешение, quality): gems}."""
    options: dict[str, tuple[str, ...]] = {"aspect_ratio": aspects}
    simple = {"aspect_ratio": "1:1" if "1:1" in aspects else aspects[0]}
    if isinstance(grid, (int, float)):
        keys: tuple[str, ...] = ()
        grid = {(): float(grid)}
    else:
        grid = {(k if isinstance(k, tuple) else (k,)): float(v) for k, v in grid.items()}
        res = tuple(dict.fromkeys(k[0] for k in grid))
        options["resolution"] = res
        simple["resolution"] = res[0]
        keys = ("resolution",)
        if quality:
            options["quality"] = quality
            keys = ("resolution", "quality")
            simple["quality"] = quality[0]
    return ModelSpec(
        key=key or f"mage:{model_id}", provider="mage", kind="image", title=title, arch=arch, model_id=model_id,
        base_units=min(grid.values()), options=options, simple=simple, image_field=image_field, note=note,
        price_keys=keys, price_grid=grid, family=family,
    )


def _mage_video(model_id: str, arch: str, title: str, rates: dict[str, float], aspects, durations,
                image_field: str | None, note: str, extra: dict | None = None, family: str = "") -> ModelSpec:
    """rates — gems в секунду по разрешению."""
    durations = _durations(tuple(durations))
    options = {"aspect_ratio": tuple(aspects), "resolution": tuple(rates), "duration": durations}
    if extra:
        options.update(extra)
    simple = {"aspect_ratio": "9:16" if "9:16" in aspects else aspects[0],
              "resolution": next(iter(rates)), "duration": "5" if "5" in durations else durations[0]}
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="video", title=title, arch=arch, model_id=model_id,
        base_units=math.ceil(next(iter(rates.values())) * float(simple["duration"]) - 1e-9), options=options,
        simple=simple, image_field=image_field, note=note, rate_by_res=rates, family=family or arch.title(),
    )


def _mage_clip(model_id: str, arch: str, title: str, prices: dict[str, float], aspects, image_field: str | None,
               note: str, family: str) -> ModelSpec:
    """Видео фиксированной длины: цена зависит только от разрешения."""
    options = {"aspect_ratio": tuple(aspects), "resolution": tuple(prices)}
    simple = {"aspect_ratio": "9:16" if "9:16" in aspects else aspects[0], "resolution": next(iter(prices))}
    return ModelSpec(
        key=f"mage:{model_id}", provider="mage", kind="video", title=title, arch=arch, model_id=model_id,
        base_units=min(prices.values()), options=options, simple=simple, image_field=image_field, note=note,
        price_keys=("resolution",), price_grid={(k,): float(v) for k, v in prices.items()}, family=family,
    )


GPT_GRID = {("1K", "low"): 9, ("1K", "medium"): 80, ("1K", "high"): 317,
            ("2K", "low"): 18, ("2K", "medium"): 160, ("2K", "high"): 634}
NANO_ASPECTS = ("1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "4:5", "5:4", "21:9", "4:1", "8:1")
GROK_ASPECTS = ("1:1", "9:16", "16:9", "3:4", "4:3", "2:3", "3:2", "2:1", "1:2")
V_STD = ("9:16", "16:9", "1:1", "3:4", "4:3")
V_WIDE = ("9:16", "16:9", "1:1", "4:5", "2:3", "3:2", "21:9")
SECONDS = tuple(str(i) for i in range(1, 31))

# Все модели Mage. Цены сняты через estimate_cost 2026-10-02: картинки — gems за штуку,
# видео — gems в секунду по разрешению (итог округляется вверх). Формат кадра на цену не влияет.
MAGE_MODELS: tuple[ModelSpec, ...] = (
    # --- картинки ---
    _mage_image("gpt-image-2.5-flare", "gpt_image_2", "GPT Image 2.5 Flare", GPT_GRID,
                "точно следует промпту, текст на картинке", quality=("low", "medium", "high")),
    _mage_image("gpt-image-2.5-sunburst", "gpt_image_2", "GPT Image 2.5 Sunburst", GPT_GRID,
                "лучший для точных правок по фото", quality=("low", "medium", "high")),
    _mage_image("gpt-image-2", "gpt_image_2", "GPT Image 2", GPT_GRID, "генерация и правки",
                quality=("low", "medium", "high")),
    _mage_image("nano-banana-v2", "nano_banana_v2", "Nano Banana 2",
                {"512": 68, "1K": 101, "2K": 152, "4K": 227}, "читаемый текст, до 14 референсов, до 4K",
                aspects=NANO_ASPECTS),
    _mage_image("mango-v3-turbo", "mango", "Mango 3 Turbo", {"1K": 27, "2K": 27}, "быстрый и дешёвый, сильный текст"),
    _mage_image("mango-v3", "mango", "Mango 3", {"1K": 68, "2K": 135}, "флагман: персонажи, точные правки"),
    _mage_image("mango-v3s", "mango", "Mango 3S", {"2K": 55, "3K": 55}, "персонажи и референсы, до 3K"),
    _mage_image("mango-v2", "mango", "Mango 2", {"2K": 60, "3K": 60, "4K": 60}, "картинки до 4K"),
    _mage_image("mango", "mango", "Mango 1", {"1K": 45, "2K": 45, "3K": 45, "4K": 45}, "классика, без референсов",
                image_field=None),
    _mage_image("guava-2", "guava", "Guava 2", {"1K": 36, "2K": 36}, "фотореализм, быстрее"),
    _mage_image("guava-2-pro", "guava", "Guava 2 Pro", {"1K": 48, "2K": 90}, "фотореализм: портреты, мода, товары"),
    _mage_image("guava-pro-v1-5", "guava", "Guava Pro 1.5", {"1K": 79, "2K": 79}, "прошлая версия"),
    _mage_image("guava-pro", "guava", "Guava Pro", {"1K": 79, "2K": 79}, "прошлая версия"),
    _mage_image("guava", "guava", "Guava 1", {"1K": 37, "2K": 37}, "прошлая версия"),
    _mage_image("grok-imagine-image", "grok_image", "Grok Image", {"1k": 30, "2k": 30}, "быстрый", aspects=GROK_ASPECTS),
    _mage_image("grok-imagine-image-quality", "grok_image", "Grok Image Quality", {"1k": 75, "2k": 105},
                "качественнее", aspects=GROK_ASPECTS),
    _mage_image("grok-imagine-image-2.0", "grok_image", "Grok Image 2.0", {"1k": 90, "2k": 120},
                "новейший Grok", aspects=GROK_ASPECTS),
    _mage_image("flux2-dev", "flux2", "Flux 2 Dev", {"1k": 40, "2k": 40}, "длинные детальные промпты, правки"),
    _mage_image("z-image-turbo", "z_image", "Z-Image Turbo", {"1k": 10, "2k": 10}, "самый быстрый, фото и надписи",
                image_field=None, family="Z Image"),
    _mage_image("krea-2-turbo", "krea_2", "Krea 2 Turbo", {"1k": 20, "2k": 20}, "сильная эстетика", image_field=None),
    _mage_image("anima-v1", "anima", "Anima v1", {"1k": 20, "2k": 20}, "аниме и иллюстрации", image_field=None),
    _mage_image("chroma-v1-hd", "chroma", "Chroma HD", 35, "универсальная открытая модель", image_field=None),
    _mage_image("hidream-fast", "hidream", "HiDream Fast", 20, "быстрая, фото или рисунок", image_field=None),
    _mage_image("sd-3-5-large", "stable_diffusion_v35_large", "Stable Diffusion 3.5 Large", 10,
                "точно следует промпту", family="Stable Diffusion"),
    _mage_image("6A35A7855770AE9820A3C931D4964C3817B6D9E3C6F9C4DABB5B3A94E5643B80", "stable_diffusion_xl",
                "Stable Diffusion XL", 10, "фотореализм", family="Stable Diffusion", key="mage:sdxl"),
    _mage_image("6A35A7855770AE9820A3C931D4964C3817B6D9E3C6F9C4DABB5B3A94E5643B80", "sdxl_plus",
                "SDXL Plus", 15, "детализация, чистка лиц и рук", family="Stable Diffusion", key="mage:sdxl-plus"),
    _mage_image("15012C538F503CE2EBFC2C8547B268C75CCDAFF7A281DB55399940FF1D70E21D", "stable_diffusion_v15",
                "Stable Diffusion 1.5", 10, "классика", family="Stable Diffusion", key="mage:sd15"),
    # --- видео ---
    _mage_video("lemon", "lemon", "Lemon", {"480p": 81.6, "720p": 168, "1080p": 336}, V_STD, SECONDS[1:],
                "first_image", "баланс цены и качества, со звуком, оживляет фото", {"audio": ("true", "false")}),
    _mage_video("cherry-mini", "cherry", "Cherry Mini", {"480p": 60, "720p": 120}, ("9:16", "16:9", "1:1"),
                ("4", "5", "8", "10", "15"), "image", "самое дешёвое кино-видео"),
    _mage_video("cherry", "cherry", "Cherry", {"480p": 90, "720p": 180}, ("9:16", "16:9", "1:1"),
                ("4", "5", "8", "10", "15"), "image", "кино-движение и звук"),
    _mage_video("cherry-pro", "cherry", "Cherry Pro", {"480p": 105, "720p": 225, "1080p": 555, "4k": 1170},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15"), "image", "до 4K"),
    _mage_video("cherry-2-pro", "cherry", "Cherry 2 Pro", {"480p": 154.5, "720p": 346.5, "1080p": 853.5},
                ("9:16", "16:9", "1:1"), ("4", "5", "8", "10", "15", "20", "25", "30"), "image",
                "флагман, до 30 сек"),
    _mage_video("berry-2", "berry", "Berry 2", {"480p": 63, "720p": 126, "1080p": 162},
                ("9:16", "16:9", "1:1", "3:4", "4:3", "4:5", "5:4"), SECONDS[2:15], "first_image",
                "точно держит референсы"),
    _mage_video("berry", "berry", "Berry 1", {"720p": 147, "1080p": 252},
                ("9:16", "16:9", "1:1", "3:4", "4:3", "4:5", "5:4"), SECONDS[2:15], "first_image", "прошлая версия"),
    _mage_video("blueberry-v2", "blueberry", "Blueberry 2", {"720p": 90, "1080p": 135}, V_STD, SECONDS[1:15],
                "first_image", "движение между первым и последним кадром, со звуком", {"audio": ("true", "false")}),
    _mage_video("blueberry", "blueberry", "Blueberry 1", {"720p": 105, "1080p": 159}, V_STD, SECONDS[1:15],
                "first_image", "прошлая версия", {"audio": ("true", "false")}),
    _mage_video("raspberry", "raspberry", "Raspberry", {"720p": 90, "1080p": 135}, V_STD, SECONDS[1:15],
                "first_image", "персонажи с голосом и звуком"),
    _mage_video("kiwi", "kiwi", "Kiwi", {"480p": 54, "720p": 105, "1080p": 159}, V_STD, ("5", "10"),
                "first_image", "простое видео из текста или фото"),
    _mage_video("melon", "melon", "Melon", {"540p": 36.8, "720p": 57.8, "1080p": 68.4}, V_STD,
                ("3", "4", "5", "6", "8", "10", "16"), "first_image", "аниме-видео"),
    _mage_video("melon-pro", "melon", "Melon Pro", {"540p": 47.4, "720p": 105, "1080p": 126}, V_STD,
                ("3", "4", "5", "6", "8", "10", "16"), "first_image", "аниме-видео, качественнее"),
    _mage_video("grok-imagine-video", "grok_video", "Grok Video", {"480p": 75, "720p": 105},
                ("9:16", "16:9", "1:1", "3:4", "4:3", "2:3", "3:2"), SECONDS[:15], "image", "видео от xAI",
                family="Grok"),
    _mage_video("minimax-h3-turbo", "minimax_h3", "MiniMax H3 Turbo", {"480p": 6, "544p": 7, "720p": 12, "768p": 15},
                V_WIDE, SECONDS[4:15], "first_image", "очень дёшево, видео со звуком", family="MiniMax"),
    _mage_video("minimax-h3", "minimax_h3", "MiniMax H3", {"480p": 18, "544p": 25, "720p": 54, "768p": 66},
                V_WIDE, ("5", "6", "7", "8"), "first_image", "видео со стереозвуком", family="MiniMax"),
    _mage_video("plum-max", "plum", "Plum Max", {"480P": 67.5, "768P": 108},
                ("9:16", "16:9", "1:1", "3:4", "4:3", "21:9"), SECONDS[4:15], "first_image", "звук в каждом ролике"),
    _mage_video("plum", "plum", "Plum", {"768P": 108, "2K": 156}, ("9:16", "16:9", "1:1", "3:4", "4:3", "21:9"),
                SECONDS[3:15], "first_image", "звук, до 2K"),
    _mage_clip("wan22-video", "wan_22", "Wan 2.2", {"240p": 40, "360p": 90, "480p": 165, "720p": 495}, V_WIDE,
               "first_image", "открытая модель, ролик ~5 сек", "Wan"),
    _mage_clip("wan22-video-lightning", "wan_22", "Wan 2.2 Lightning", {"240p": 35, "360p": 75, "480p": 140,
               "720p": 415}, V_WIDE, "first_image", "быстрее", "Wan"),
    _mage_clip("ltx-video-096-distilled", "ltx_video", "LTX Video Distilled", {"240p": 10, "360p": 15, "480p": 20,
               "720p": 25}, V_WIDE, "first_image", "самое дешёвое видео для черновиков", "LTX"),
    _mage_clip("ltx-video-096-dev", "ltx_video", "LTX Video Dev", {"240p": 10, "360p": 15, "480p": 20, "720p": 25},
               V_WIDE, "first_image", "черновики", "LTX"),
)

# Доплата Mage за фото-референс (снято через estimate_cost): gems сверху или множитель.
MAGE_IMAGE_SURCHARGE = {
    "mage:gpt-image-2.5-flare": 15, "mage:gpt-image-2.5-sunburst": 15, "mage:gpt-image-2": 15,
    "mage:guava-2": 5, "mage:guava-2-pro": 5,
    "mage:grok-imagine-image": 3, "mage:grok-imagine-image-quality": 15, "mage:grok-imagine-image-2.0": 15,
    "mage:flux2-dev": 5, "mage:sd-3-5-large": 5, "mage:grok-imagine-video": 3,
}
MAGE_IMAGE_MULTIPLIER = {  # стартовый кадр у Wan и LTX дороже примерно на 20%
    "mage:wan22-video": 1.21, "mage:wan22-video-lightning": 1.21,
    "mage:ltx-video-096-distilled": 1.21, "mage:ltx-video-096-dev": 1.21,
}
BOOL = ("true", "false")
NEG = ("negative_prompt",)


def _steps(values: dict[str, float | tuple[float, float]]) -> dict:
    """Шаги генерации: {значение: +gems} или {значение: (+gems, ×множитель)}."""
    return {"num_inference_steps": {k: (v if isinstance(v, tuple) else (float(v), 1.0)) for k, v in values.items()}}


# Продвинутые параметры Mage по архитектуре (из get_model) и их влияние на цену (снято через estimate_cost).
# Что не меняет цену или только удешевляет (детейлер SDXL Plus выкл.), не учитываем: разница вернётся по факту.
MAGE_EXTRAS: dict[str, dict] = {
    "nano_banana_v2": {"options": {"thinking_level": ("minimal", "high"), "web_search": BOOL, "image_search": BOOL},
                       "prices": {"web_search": {"true": (20, 1.0)}, "image_search": {"true": (20, 1.0)}}},
    "guava": {"options": {"prompt_extend": BOOL}},
    "lemon": {"options": {"prompt_extend": BOOL}},
    "blueberry": {"options": {"prompt_extend": BOOL}},
    "melon": {"options": {"generate_audio": BOOL}, "prices": {"generate_audio": {"true": (79, 1.0)}}},
    "flux2": {"options": {"num_inference_steps": ("20", "28", "40", "50"), "guidance_scale": ("2", "4", "7")},
              "prices": _steps({"20": -10, "40": 10, "50": 20})},
    "z_image": {"options": {"num_inference_steps": ("9", "18")}},
    "krea_2": {"options": {"num_inference_steps": ("8", "16")}, "prices": _steps({"16": 5})},
    "anima": {"options": {"num_inference_steps": ("30", "60"), "guidance_scale": ("3", "5", "8"),
                          "prompt_weighting": BOOL}, "prices": _steps({"60": 5})},
    "chroma": {"options": {"num_inference_steps": ("40", "80"), "guidance_scale": ("2", "3", "5")},
               "prices": _steps({"80": 20})},
    "hidream": {"options": {"num_inference_steps": ("16", "32")}, "prices": _steps({"32": 10}), "text": NEG},
    "stable_diffusion_v35_large": {"options": {"num_inference_steps": ("28", "56"),
                                               "guidance_scale": ("4.5", "7.5", "10")},
                                   "prices": _steps({"56": 5}), "text": NEG},
    "stable_diffusion_xl": {"options": {"num_inference_steps": ("30", "60"), "guidance_scale": ("4", "7", "10")},
                            "text": NEG},
    "sdxl_plus": {"options": {"num_inference_steps": ("50", "100"), "guidance_scale": ("3", "5", "8"),
                              "hires": BOOL, "adetailer_face": BOOL, "adetailer_hands": BOOL},
                  "prices": _steps({"100": 5}), "text": NEG},
    "stable_diffusion_v15": {"options": {"num_inference_steps": ("20", "40"), "guidance_scale": ("5", "7.5", "10")},
                             "text": NEG},
    # Wan: 30 шагов ≈ ×2 на любом разрешении (480p 165→325, 720p 495→985).
    "wan_22": {"options": {"num_inference_steps": ("15", "30"), "guidance_scale": ("2", "4", "6")},
               "prices": _steps({"30": (0, 2.0)}), "text": NEG},
    # LTX: 16 шагов дороже в 1.25–1.8 раза в зависимости от разрешения — берём верх.
    "ltx_video": {"options": {"num_inference_steps": ("8", "16"), "guidance_scale": ("2", "3", "5"),
                              "prompt_enhance": BOOL}, "prices": _steps({"16": (0, 1.8)}), "text": NEG},
}


def _with_extras(m: ModelSpec) -> ModelSpec:
    extra = MAGE_EXTRAS.get(m.arch, {})
    return dataclasses.replace(
        m, image_surcharge=float(MAGE_IMAGE_SURCHARGE.get(m.key, 0)),
        image_multiplier=MAGE_IMAGE_MULTIPLIER.get(m.key, 1.0),
        options={**m.options, **extra.get("options", {})},
        param_prices={k: {v: (float(a), float(x)) for v, (a, x) in t.items()} for k, t in extra.get("prices", {}).items()},
        text_params=tuple(extra.get("text", ())),
    )


MAGE_MODELS = tuple(_with_extras(m) for m in MAGE_MODELS)

MOCK_MODELS: tuple[ModelSpec, ...] = (
    ModelSpec("mock:image", "mock", "image", "Тестовая картинка", "mock", base_units=6,
              options={"aspect_ratio": ("1:1", "9:16")}, simple={"aspect_ratio": "1:1"}, image_field="image",
              text_params=NEG),
    ModelSpec("mock:video", "mock", "video", "Тестовое видео", "mock", base_units=60,
              options={"duration": ("5", "10")}, base={"duration": "5"}, simple={"duration": "5"},
              image_field="image"),
)


KNOWN_FAMILIES = (
    "Nano Banana", "GPT Image", "GPT-4o Image", "Seedance", "Seedream", "Flux", "Qwen Image", "Z Image",
    "Grok Imagine", "Midjourney", "Kling AI Avatar", "Kling", "Veo", "Hailuo", "Wan", "Sora", "Ideogram", "Recraft",
    "Mango", "Guava", "Lemon", "Cherry", "Berry", "Blueberry", "Raspberry", "Kiwi", "Melon", "Plum", "MiniMax",
    "Krea", "Anima", "Chroma", "HiDream", "Stable Diffusion", "SDXL", "LTX",
)
PRICE_PARAMS = ("resolution", "quality", "effort", "duration")


def family_of(spec: ModelSpec) -> str:
    """Семейство модели: явное, из известных префиксов или название до номера версии."""
    if spec.family:
        return spec.family
    low = spec.title.lower()
    for fam in KNOWN_FAMILIES:
        if low == fam.lower() or low.startswith(fam.lower() + " "):
            return fam
    words = []
    for w in spec.title.split():
        if any(ch.isdigit() for ch in w):
            break
        words.append(w)
    return " ".join(words) or spec.title


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

    def estimate_units(self, spec: ModelSpec, params: dict[str, str], with_image: bool = False) -> float:
        """Стоимость у провайдера (gems / кредиты Vilva) при выбранных параметрах (и с фото-референсом)."""
        units = self._units(spec, params)
        add, mult = 0.0, 1.0
        for name, table in spec.param_prices.items():
            a, x = table.get(params.get(name, ""), (0.0, 1.0))
            add, mult = add + a, mult * x
        if add or mult != 1.0:
            units = float(math.ceil(max(units + add, 0) * mult - 1e-9))
        if with_image and (spec.image_surcharge or spec.image_multiplier != 1.0):
            units = float(math.ceil((units + spec.image_surcharge) * spec.image_multiplier - 1e-9))
        return units

    def _units(self, spec: ModelSpec, params: dict[str, str]) -> float:
        p = {**spec.simple, **params}
        if spec.rate_by_res:
            rates = spec.rate_by_res
            res = p.get("resolution", "")
            # Неизвестное разрешение — по самой дорогой ставке: лишнее вернётся по факту, в минус не уйдём.
            rate = rates.get(res) or rates.get(res.lower()) or rates.get("default") or max(rates.values())
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

    def price_range(self, spec: ModelSpec) -> tuple[int, int]:
        """Минимальная и максимальная цена в кредитах по всем параметрам, влияющим на цену."""
        import itertools

        keys = [k for k in (*PRICE_PARAMS, *spec.param_prices) if spec.options.get(k)]
        prices = [self.price(spec, dict(zip(keys, combo)))
                  for combo in itertools.product(*(spec.options[k] for k in keys))] if keys else []
        if not prices:
            prices = [self.price(spec, {})]
        return min(prices), max(prices)

    def price(self, spec: ModelSpec, params: dict[str, str], with_image: bool = False) -> int:
        return self.credits(spec, self.estimate_units(spec, params, with_image))


def build_catalog(cfg: Config) -> Catalog:
    models: list[ModelSpec] = []
    if cfg.mage_key:
        models.extend(MAGE_MODELS)
    if cfg.use_mock:
        models.extend(MOCK_MODELS)
    return Catalog(cfg, models)
