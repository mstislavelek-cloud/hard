"""Настройки бота из переменных окружения (и файла .env)."""
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


# Пакеты нарисованы на bot/assets/prices.jpg — при изменении перегенерировать картинку.
DEFAULT_PACKS = (
    Pack("s", 100, 40, "Старт — 40 кредитов"),
    Pack("m", 250, 120, "Оптимальный — 120 кредитов"),
    Pack("l", 600, 320, "Профи — 320 кредитов"),
    Pack("xl", 1500, 900, "Студия — 900 кредитов"),
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    brand: str = "Mirage"
    telegram_proxy: str = ""
    db_path: str = "genbot.sqlite3"
    mage_key: str = ""
    mage_base_url: str = "https://api.mage.space/v1"
    vilva_key: str = ""
    vilva_url: str = "https://api.vilva.ai/mcp"
    # Себестоимость единиц провайдеров в $: Mage 10 000 gems = $10, Vilva 4 000 кредитов = $21.
    mage_usd_per_gem: float = 0.001
    vilva_usd_per_credit: float = 21 / 4000
    # Сколько $ приносит 1 кредит бота: пакет 100⭐ = 40 кр, 1000⭐ ≈ $13.3 → ≈ $0.033 за кредит.
    credit_usd: float = 0.03325
    # Наценка поверх себестоимости.
    markup: float = 1.6
    default_image_model: str = "mage:gpt-image-2.5-flare"
    default_video_model: str = "mage:lemon"
    # Бюджеты агента в кредитах бота, из которых выбирает пользователь.
    agent_budgets: tuple[int, ...] = (100, 300, 800)
    free_credits: int = 3  # «3 кредита в подарок» нарисовано на баннере bot/assets/welcome.jpg
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    support_contact: str = "@support"
    packs: tuple[Pack, ...] = DEFAULT_PACKS
    # Тестовые модели без затрат, если не задан ни один ключ.
    use_mock: bool = False


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


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
    budgets = tuple(
        int(x) for x in os.getenv("AGENT_BUDGETS", "").replace(" ", "").split(",") if x
    ) or Config.agent_budgets
    mage_key = os.getenv("MAGE_API_KEY", "")
    vilva_key = os.getenv("VILVA_API_KEY", "")
    return Config(
        bot_token=token,
        brand=os.getenv("BOT_BRAND", Config.brand),
        telegram_proxy=os.getenv("TELEGRAM_PROXY", ""),
        db_path=os.getenv("DB_PATH", "genbot.sqlite3"),
        mage_key=mage_key,
        mage_base_url=os.getenv("MAGE_BASE_URL", Config.mage_base_url),
        vilva_key=vilva_key,
        vilva_url=os.getenv("VILVA_MCP_URL", Config.vilva_url),
        mage_usd_per_gem=_float("MAGE_USD_PER_GEM", Config.mage_usd_per_gem),
        vilva_usd_per_credit=_float("VILVA_USD_PER_CREDIT", Config.vilva_usd_per_credit),
        credit_usd=_float("CREDIT_USD", Config.credit_usd),
        markup=_float("MARKUP", Config.markup),
        default_image_model=os.getenv("DEFAULT_IMAGE_MODEL", Config.default_image_model),
        default_video_model=os.getenv("DEFAULT_VIDEO_MODEL", Config.default_video_model),
        agent_budgets=budgets,
        free_credits=_int("FREE_CREDITS", 3),
        admin_ids=admins,
        support_contact=os.getenv("SUPPORT_CONTACT", "@support"),
        use_mock=os.getenv("USE_MOCK", "") == "1" or not (mage_key or vilva_key),
    )
