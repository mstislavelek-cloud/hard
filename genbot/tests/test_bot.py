from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from bot import handlers
from bot.config import Config
from bot.db import Database, InsufficientCredits
from bot.main import build_dispatcher
from bot.moderation import check_prompt
from bot.providers.base import Media
from bot.providers.mock import MockProvider
from bot.providers.openrouter import parse_image_url


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite3"))
    yield d
    d.close()


@pytest.fixture
def cfg():
    return Config(bot_token="123:abc", admin_ids=frozenset({1}))


class FakeMessage:
    def __init__(self, user_id=42, text="кот в очках"):
        self.from_user = SimpleNamespace(id=user_id, username="u")
        self.text = text
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text, **kw):
        self.sent.append(("text", text))
        return SimpleNamespace(delete=self._noop)

    async def answer_photo(self, file, **kw):
        self.sent.append(("photo", file))

    async def answer_video(self, file, **kw):
        self.sent.append(("video", file))

    async def answer_document(self, file, **kw):
        self.sent.append(("document", file))

    async def _noop(self):
        return None


# --- база ---

def test_new_user_gets_free_credits_once(db):
    assert db.ensure_user(1, "a", 3) is True
    assert db.ensure_user(1, "a", 3) is False
    assert db.balance(1) == 3


def test_charge_and_refund_on_failure(db):
    db.ensure_user(1, "a", 3)
    gen = db.charge(1, "image", "p", 2)
    assert db.balance(1) == 1
    db.finish(gen, ok=False)
    assert db.balance(1) == 3
    db.finish(gen, ok=False)  # повторное закрытие не возвращает дважды
    assert db.balance(1) == 3


def test_charge_insufficient(db):
    db.ensure_user(1, "a", 1)
    with pytest.raises(InsufficientCredits):
        db.charge(1, "video", "p", 15)
    assert db.balance(1) == 1


def test_payment_idempotent_and_refund(db):
    db.ensure_user(1, "a", 0)
    assert db.add_payment("ch1", 1, 100, 20) is True
    assert db.add_payment("ch1", 1, 100, 20) is False
    assert db.balance(1) == 20
    db.mark_refunded("ch1")
    db.mark_refunded("ch1")
    assert db.balance(1) == 0
    assert db.get_payment("ch1")[3] == 1
    assert db.stats()["stars"] == 0


# --- модерация ---

@pytest.mark.parametrize("prompt", ["nude girl", "ПОРНО", "раздень её", "teen model"])
def test_blocked_prompts(prompt):
    assert check_prompt(prompt) is not None


def test_allowed_and_empty_prompt():
    assert check_prompt("кроссовки на белом фоне") is None
    assert check_prompt("   ") is not None
    assert check_prompt("x" * 2000) is not None


# --- провайдеры ---

def test_parse_data_url():
    m = parse_image_url("data:image/png;base64,aGVsbG8=")
    assert m.data == b"hello"
    assert parse_image_url("https://x/y.png").url == "https://x/y.png"


# --- оплата ---

def test_pre_checkout_validates_amount(cfg):
    answers = []

    async def answer(ok, error_message=None):
        answers.append(ok)

    good = SimpleNamespace(invoice_payload="pack:s", currency="XTR", total_amount=100, answer=answer)
    bad = SimpleNamespace(invoice_payload="pack:s", currency="XTR", total_amount=1, answer=answer)
    asyncio.run(handlers.pre_checkout(good, cfg))
    asyncio.run(handlers.pre_checkout(bad, cfg))
    assert answers == [True, False]


def test_successful_payment_credits_once(db, cfg):
    msg = FakeMessage()
    msg.successful_payment = SimpleNamespace(
        invoice_payload="pack:m", telegram_payment_charge_id="c1", total_amount=250
    )
    asyncio.run(handlers.on_payment(msg, db, cfg))
    asyncio.run(handlers.on_payment(msg, db, cfg))
    assert db.balance(42) == cfg.free_credits + 60


# --- генерация ---

def _deps(db, cfg, provider):
    return dict(db=db, cfg=cfg, image_provider=provider, video_provider=provider, busy=set())


def test_generate_image_charges_and_sends(db, cfg):
    msg = FakeMessage()
    provider = MockProvider()
    asyncio.run(handlers.generate(msg, "image", "кот", **_deps(db, cfg, provider)))
    assert ("image", "кот") in provider.calls
    assert any(kind == "photo" for kind, _ in msg.sent)
    assert db.balance(42) == cfg.free_credits - cfg.image_cost


def test_generate_failure_refunds(db, cfg):
    msg = FakeMessage()
    asyncio.run(handlers.generate(msg, "image", "кот", **_deps(db, cfg, MockProvider(fail=True))))
    assert db.balance(42) == cfg.free_credits
    assert "кредиты вернул" in msg.sent[-1][1]


def test_generate_video_without_credits(db, cfg):
    msg = FakeMessage()
    provider = MockProvider()
    asyncio.run(handlers.generate(msg, "video", "волны", **_deps(db, cfg, provider)))
    assert provider.calls == []
    assert "Не хватает кредитов" in msg.sent[-1][1]


def test_generate_blocked_prompt_not_charged(db, cfg):
    msg = FakeMessage()
    provider = MockProvider()
    asyncio.run(handlers.generate(msg, "image", "nude", **_deps(db, cfg, provider)))
    assert provider.calls == []
    assert db.balance(42) == cfg.free_credits


def test_send_media_video_mp4():
    msg = FakeMessage()
    asyncio.run(handlers.send_media(msg, "video", Media(url="https://x/v.mp4", filename="v.mp4")))
    assert msg.sent[0][0] == "video"


def test_dispatcher_builds(db, cfg):
    dp = build_dispatcher(cfg, db)
    assert dp["busy"] == set()


def test_load_dotenv(tmp_path, monkeypatch):
    from bot.config import load_dotenv

    env = tmp_path / ".env"
    env.write_text('# c\nGENBOT_X="1"\nGENBOT_Y=2\n', encoding="utf-8")
    monkeypatch.setenv("GENBOT_Y", "keep")
    monkeypatch.delenv("GENBOT_X", raising=False)
    load_dotenv(str(env))
    import os

    assert os.environ["GENBOT_X"] == "1" and os.environ["GENBOT_Y"] == "keep"
    monkeypatch.delenv("GENBOT_X")
