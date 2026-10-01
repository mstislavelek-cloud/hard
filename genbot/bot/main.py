"""Точка входа: python -m bot.main"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from .config import Config, load_config
from .db import Database
from .handlers import create_router
from .providers import build_providers


def build_dispatcher(cfg: Config, db: Database) -> Dispatcher:
    image_provider, video_provider = build_providers(cfg)
    dp = Dispatcher(
        db=db,
        cfg=cfg,
        image_provider=image_provider,
        video_provider=video_provider,
        busy=set(),
    )
    dp.include_router(create_router())
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = Database(cfg.db_path)
    bot = Bot(cfg.bot_token)
    dp = build_dispatcher(cfg, db)
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Начать"),
            BotCommand(command="video", description="Сгенерировать видео"),
            BotCommand(command="balance", description="Баланс"),
            BotCommand(command="buy", description="Пополнить"),
            BotCommand(command="terms", description="Условия"),
            BotCommand(command="paysupport", description="Помощь с оплатой"),
        ]
    )
    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
