"""Прогон апдейтов через настоящий Dispatcher с фейковой сессией Bot (без сети)."""
from __future__ import annotations

import asyncio
import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerPreCheckoutQuery, DeleteMessage, SendInvoice, TelegramMethod
from aiogram.types import Message, Update

from bot.config import Config
from bot.db import Database
from bot.main import build_dispatcher


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (DeleteMessage, AnswerPreCheckoutQuery)):
            return True
        return Message(
            message_id=len(self.calls),
            date=datetime.datetime.now(),
            chat={"id": 42, "type": "private"},
        )

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):
        yield b""


def _msg(text, **extra):
    return {
        "message_id": 1,
        "date": 0,
        "chat": {"id": 42, "type": "private"},
        "from": {"id": 42, "is_bot": False, "first_name": "U"},
        "text": text,
        **extra,
    }


def run(updates, tmp_path):
    db = Database(str(tmp_path / "r.sqlite3"))
    cfg = Config(bot_token="123:abc")
    session = FakeSession()
    bot = Bot("123:abc", session=session)
    dp = build_dispatcher(cfg, db)

    async def go():
        for i, u in enumerate(updates):
            await dp.feed_update(bot, Update.model_validate({"update_id": i, **u}, context={"bot": bot}))

    asyncio.run(go())
    return db, session


def names(session):
    return [type(c).__name__ for c in session.calls]


def test_start_then_text_generates_photo(tmp_path):
    db, s = run([{"message": _msg("/start")}, {"message": _msg("кот в очках")}], tmp_path)
    assert "SendPhoto" in names(s)
    assert db.balance(42) == 2


def test_buy_callback_sends_stars_invoice(tmp_path):
    cb = {"callback_query": {"id": "1", "from": {"id": 42, "is_bot": False, "first_name": "U"},
                             "chat_instance": "x", "data": "buy:s", "message": _msg("x")}}
    _, s = run([{"message": _msg("/start")}, cb], tmp_path)
    inv = [c for c in s.calls if isinstance(c, SendInvoice)][0]
    assert inv.currency == "XTR" and inv.prices[0].amount == 100


def test_payment_flow(tmp_path):
    pcq = {"pre_checkout_query": {"id": "p", "from": {"id": 42, "is_bot": False, "first_name": "U"},
                                  "currency": "XTR", "total_amount": 100, "invoice_payload": "pack:s"}}
    paid = {"message": {**_msg(None), "successful_payment": {
        "currency": "XTR", "total_amount": 100, "invoice_payload": "pack:s",
        "telegram_payment_charge_id": "tg1", "provider_payment_charge_id": "p1"}}}
    db, s = run([{"message": _msg("/start")}, pcq, paid, paid], tmp_path)
    ans = [c for c in s.calls if isinstance(c, AnswerPreCheckoutQuery)][0]
    assert ans.ok is True
    assert db.balance(42) == 3 + 20


def test_video_needs_credits(tmp_path):
    db, s = run([{"message": _msg("/start")}, {"message": _msg("/video волны")}], tmp_path)
    assert "SendVideo" not in names(s) and "SendDocument" not in names(s)
    assert db.balance(42) == 3
