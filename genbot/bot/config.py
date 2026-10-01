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


DEFAULT_PACKS = (
    Pack("s", 100, 20, "Старт — 20 кредитов"),
    Pack("m", 250, 60, "Оптимальный — 60 кредитов"),
    Pack("l", 600, 160, "Профи — 160 кредитов"),
    Pack("xl", 1500, 450, "Студия — 450 кредитов"),
)


@dataclass(frozen=True)
class Config:
    bot_token: str
    db_path: str = "genbot.sqlite3"
    mage_key: str = ""
    mage_base_url: str = "https://api.mage.space/v1"
    vilva_key: str = ""
    vilva_url: str = "https://api.vilva.ai/mcp"
    # Сколько gems Mage стоит один кредит бота (до наценки). 1 кредит ≈ $0.066 при пакете 100⭐/20 кр.
    gems_per_credit: float = 6.0
    # Сколько кредитов Vilva стоит один кредит бота (до наценки).
    vilva_credits_per_credit: float = 1.0
    # Наценка поверх себестоимости.
    markup: float = 2.0
    default_image_model: str = "mage:gpt-image-2.5-flare"
    default_video_model: str = "mage:lemon"
    # Бюджеты агента в кредитах бота, из которых выбирает пользователь.
    agent_budgets: tuple[int, ...] = (50, 150, 400)
    free_credits: int = 3
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
        db_path=os.getenv("DB_PATH", "genbot.sqlite3"),
        mage_key=mage_key,
        mage_base_url=os.getenv("MAGE_BASE_URL", Config.mage_base_url),
        vilva_key=vilva_key,
        vilva_url=os.getenv("VILVA_MCP_URL", Config.vilva_url),
        gems_per_credit=_float("GEMS_PER_CREDIT", Config.gems_per_credit),
        vilva_credits_per_credit=_float("VILVA_CREDITS_PER_CREDIT", Config.vilva_credits_per_credit),
        markup=_float("MARKUP", Config.markup),
        default_image_model=os.getenv("DEFAULT_IMAGE_MODEL", Config.default_image_model),
        default_video_model=os.getenv("DEFAULT_VIDEO_MODEL", Config.default_video_model),
        agent_budgets=budgets,
        free_credits=_int("FREE_CREDITS", 3),
        admin_ids=admins,
        support_contact=os.getenv("SUPPORT_CONTACT", "@support"),
        use_mock=os.getenv("USE_MOCK", "") == "1" or not (mage_key or vilva_key),
    )
