"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand

from .catalog import build_catalog
from .config import Config, load_config
from .db import Database
from .handlers import App, create_router, monitor_agent
from .providers import build_providers

log = logging.getLogger(__name__)


def build_app(cfg: Config, db: Database) -> App:
    return App(cfg=cfg, db=db, catalog=build_catalog(cfg), providers=build_providers(cfg))


def build_dispatcher(app: App) -> Dispatcher:
    dp = Dispatcher(app=app)
    dp.include_router(create_router())
    return dp


async def load_vilva_models(app: App) -> None:
    if not app.vilva:
        return
    try:
        models = await app.vilva.discover_models()
        app.catalog.add(models)
        log.info("Vilva: загружено моделей %d", len(models))
    except Exception:
        log.exception("Не удалось загрузить модели Vilva (генерация через Mage работает)")


def resume_agent_runs(app: App, bot: Bot) -> None:
    """После перезапуска доотслеживаем незавершённые запуски агента."""
    if not app.vilva:
        return
    for run_id, user_id, gen_id in app.db.pending_agent_runs():
        app.spawn(monitor_agent(app, bot, user_id, run_id, gen_id, app.db.generation_cost(gen_id)))


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = Database(cfg.db_path)
    app = build_app(cfg, db)
    await load_vilva_models(app)
    # TELEGRAM_PROXY — если api.telegram.org недоступен напрямую (http://… или socks5://…, для socks нужен aiohttp-socks).
    session = AiohttpSession(proxy=cfg.telegram_proxy) if cfg.telegram_proxy else None
    bot = Bot(cfg.bot_token, session=session) if session else Bot(cfg.bot_token)
    dp = build_dispatcher(app)
    commands = [
        BotCommand(command="start", description="Начать"),
        BotCommand(command="settings", description="Модели и параметры"),
        BotCommand(command="balance", description="Баланс"),
        BotCommand(command="buy", description="Пополнить"),
        BotCommand(command="terms", description="Условия"),
        BotCommand(command="paysupport", description="Помощь с оплатой"),
    ]
    if app.vilva:
        commands.insert(2, BotCommand(command="agent", description="Креативный агент"))
    await bot.set_my_commands(commands)
    resume_agent_runs(app, bot)
    try:
        await dp.start_polling(bot)
    finally:
        for p in app.providers.values():
            close = getattr(p, "close", None)
            if close:
                await close()
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
