from __future__ import annotations

import asyncio
import datetime
import json

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    GetFile,
    TelegramMethod,
)
from aiogram.types import File, Message, Update
from aiohttp import web

from bot.catalog import MOCK_MODELS, Catalog
from bot.config import Config
from bot.db import Database
from bot.handlers import App
from bot.main import build_dispatcher
from bot.providers.mock import MockProvider


class FakeSession(BaseSession):
    """Сессия Bot без сети: запоминает вызовы API."""

    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (DeleteMessage, AnswerPreCheckoutQuery, AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path="photos/p.jpg")
        return Message(message_id=len(self.calls), date=datetime.datetime.now(), chat={"id": 42, "type": "private"})

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b"\xff\xd8"

    def texts(self) -> list[str]:
        return [getattr(c, "text", None) or "" for c in self.calls]

    def names(self) -> list[str]:
        return [type(c).__name__ for c in self.calls]


class Harness:
    def __init__(self, tmp_path, cfg: Config | None = None, providers=None, models=None):
        self.cfg = cfg or Config(bot_token="123:abc", admin_ids=frozenset({1}), use_mock=True,
                                 default_image_model="mock:image", default_video_model="mock:video")
        self.db = Database(str(tmp_path / "t.sqlite3"))
        self.provider = MockProvider()
        self.app = App(cfg=self.cfg, db=self.db, catalog=Catalog(self.cfg, list(models or MOCK_MODELS)),
                       providers=providers if providers is not None else {"mock": self.provider}, agent_poll=0.01)
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = build_dispatcher(self.app)
        self._n = 0

    def _next(self):
        self._n += 1
        return self._n

    def msg(self, text=None, uid=42, **extra):
        m = {"message_id": self._next(), "date": 0, "chat": {"id": uid, "type": "private"},
             "from": {"id": uid, "is_bot": False, "first_name": "U"}, **extra}
        if text is not None:
            m["text"] = text
        return {"message": m}

    def cb(self, data, uid=42):
        return {"callback_query": {"id": str(self._next()), "from": {"id": uid, "is_bot": False, "first_name": "U"},
                                   "chat_instance": "x", "data": data,
                                   "message": self.msg("x", uid)["message"]}}

    async def afeed(self, *updates):
        for u in updates:
            await self.dp.feed_update(self.bot, Update.model_validate({"update_id": self._next(), **u},
                                                                      context={"bot": self.bot}))

    async def drain(self):
        """Дождаться фоновых задач (мониторинг агента)."""
        while self.app.tasks:
            await asyncio.gather(*list(self.app.tasks))

    def feed(self, *updates):
        async def go():
            await self.afeed(*updates)
            await self.drain()
        asyncio.run(go())


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.db.close()


async def serve(app: web.Application, fn):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        return await fn(f"http://127.0.0.1:{port}")
    finally:
        await runner.cleanup()


def sse(msg: dict) -> web.Response:
    return web.Response(text=f"event: message\ndata: {json.dumps(msg)}\n\n", content_type="text/event-stream")
