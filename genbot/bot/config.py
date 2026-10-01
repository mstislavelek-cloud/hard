"""Настройки бота из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pack:
    """Пакет кредитов, который продаётся за Telegram Stars."""

    id: str
    stars: int
    credits: int
    title: str


DEFAULT_PACKS = (
    Pack("s", 100, 20, "Старт — 20 кредитов"),
    Pack("m", 250, 60, "Оптимальный — 60 кредитов"),
    Pack("l", 600, 160, "Профи — 160 кредитов"),
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str = "genbot.sqlite3"
    image_provider: str = "mock"
    video_provider: str = "mock"
    fal_key: str = ""
    openrouter_key: str = ""
    fal_image_model: str = "fal-ai/flux/schnell"
    fal_video_model: str = "fal-ai/kling-video/v2.1/standard/text-to-video"
    fal_i2v_model: str = "fal-ai/kling-video/v2.1/standard/image-to-video"
    openrouter_image_model: str = "google/gemini-2.5-flash-image"
    mage_key: str = ""
    mage_base_url: str = "https://api.mage.space"
    mage_submit_path: str = "/v1/generate"
    mage_status_path: str = "/v1/requests/{id}"
    mage_image_arch: str = "gpt_image_2"
    mage_image_model: str = "gpt-image-2.5-flare"
    mage_video_arch: str = "cherry"
    mage_video_model: str = "cherry-mini"
    vilva_key: str = ""
    vilva_url: str = "https://api.vilva.ai/mcp"
    vilva_image_model: str = ""
    vilva_video_model: str = ""
    image_cost: int = 1
    video_cost: int = 15
    free_credits: int = 3
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    support_contact: str = "@support"
    packs: tuple[Pack, ...] = DEFAULT_PACKS


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def load_dotenv(path: str = ".env") -> None:
    """Подхватывает KEY=VALUE из .env, не перетирая уже заданные переменные."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан")
    admins = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    return Config(
        bot_token=token,
        db_path=os.getenv("DB_PATH", "genbot.sqlite3"),
        image_provider=os.getenv("IMAGE_PROVIDER", "mock"),
        video_provider=os.getenv("VIDEO_PROVIDER", "mock"),
        fal_key=os.getenv("FAL_KEY", ""),
        openrouter_key=os.getenv("OPENROUTER_API_KEY", ""),
        fal_image_model=os.getenv("FAL_IMAGE_MODEL", Config.fal_image_model),
        fal_video_model=os.getenv("FAL_VIDEO_MODEL", Config.fal_video_model),
        fal_i2v_model=os.getenv("FAL_I2V_MODEL", Config.fal_i2v_model),
        openrouter_image_model=os.getenv(
            "OPENROUTER_IMAGE_MODEL", Config.openrouter_image_model
        ),
        mage_key=os.getenv("MAGE_API_KEY", ""),
        mage_base_url=os.getenv("MAGE_BASE_URL", Config.mage_base_url),
        mage_submit_path=os.getenv("MAGE_SUBMIT_PATH", Config.mage_submit_path),
        mage_status_path=os.getenv("MAGE_STATUS_PATH", Config.mage_status_path),
        mage_image_arch=os.getenv("MAGE_IMAGE_ARCH", Config.mage_image_arch),
        mage_image_model=os.getenv("MAGE_IMAGE_MODEL", Config.mage_image_model),
        mage_video_arch=os.getenv("MAGE_VIDEO_ARCH", Config.mage_video_arch),
        mage_video_model=os.getenv("MAGE_VIDEO_MODEL", Config.mage_video_model),
        vilva_key=os.getenv("VILVA_API_KEY", ""),
        vilva_url=os.getenv("VILVA_MCP_URL", Config.vilva_url),
        vilva_image_model=os.getenv("VILVA_IMAGE_MODEL", ""),
        vilva_video_model=os.getenv("VILVA_VIDEO_MODEL", ""),
        image_cost=_int("IMAGE_COST", 1),
        video_cost=_int("VIDEO_COST", 15),
        free_credits=_int("FREE_CREDITS", 3),
        admin_ids=admins,
        support_contact=os.getenv("SUPPORT_CONTACT", "@support"),
    )
