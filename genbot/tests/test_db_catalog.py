from __future__ import annotations

import pytest

from bot.catalog import MAGE_MODELS, Catalog
from bot.config import Config
from bot.db import Database, InsufficientCredits
from bot.moderation import check_prompt


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite3"))
    yield d
    d.close()


def test_free_credits_once(db):
    assert db.ensure_user(1, "a", 3) is True
    assert db.ensure_user(1, "a", 3) is False
    assert db.balance(1) == 3


def test_charge_refund_and_double_finish(db):
    db.ensure_user(1, "a", 10)
    gen = db.charge(1, "image", "p", 4)
    assert db.balance(1) == 6
    assert db.finish(gen, False) == 0
    assert db.balance(1) == 10
    db.finish(gen, False)
    assert db.balance(1) == 10


def test_finish_adjusts_to_actual_cost(db):
    db.ensure_user(1, "a", 10)
    gen = db.charge(1, "video", "p", 6)
    assert db.finish(gen, True, 4) == 4      # вернули 2
    assert db.balance(1) == 6
    gen = db.charge(1, "video", "p", 5)      # баланс 1
    assert db.finish(gen, True, 9) == 6      # доплата ограничена остатком
    assert db.balance(1) == 0


def test_insufficient(db):
    db.ensure_user(1, "a", 1)
    with pytest.raises(InsufficientCredits):
        db.charge(1, "video", "p", 15)


def test_payment_idempotent_and_refund(db):
    db.ensure_user(1, "a", 0)
    assert db.add_payment("ch1", 1, 100, 20) is True
    assert db.add_payment("ch1", 1, 100, 20) is False
    assert db.balance(1) == 20
    db.mark_refunded("ch1")
    db.mark_refunded("ch1")
    assert db.balance(1) == 0 and db.stats()["stars"] == 0


def test_settings_and_agent_runs(db):
    db.ensure_user(1, "a", 100)
    assert db.get_settings(1) == {}
    db.save_settings(1, {"advanced": True, "params": {"m": {"seed": "7"}}})
    assert db.get_settings(1)["params"]["m"]["seed"] == "7"
    gen = db.charge(1, "agent", "brief", 50)
    db.add_agent_run("run-1", 1, gen)
    assert db.agent_run_by_gen(gen) == ("run-1", 1)
    assert db.pending_agent_runs() == [("run-1", 1, gen)]
    db.finish(gen, True, 20)
    assert db.pending_agent_runs() == []


def test_settings_column_migration(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, credits INTEGER NOT NULL DEFAULT 0,"
                 " created_at INTEGER NOT NULL)")
    conn.execute("INSERT INTO users VALUES (1, 'a', 5, 0)")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.get_settings(1) == {} and db.balance(1) == 5
    db.close()


# --- каталог и цены ---

def test_price_scales_with_duration_and_resolution():
    cfg = Config(bot_token="x", mage_key="k", gems_per_credit=6, markup=2)
    cat = Catalog(cfg, list(MAGE_MODELS))
    lemon = cat.get("mage:lemon")
    base = cat.price(lemon, {"duration": "3", "resolution": "480p"})
    assert base == 82  # 245 gems * 2 / 6
    assert cat.price(lemon, {"duration": "6", "resolution": "480p"}) == 164
    assert cat.price(lemon, {"duration": "3", "resolution": "1080p"}) > base * 3
    flare = cat.get("mage:gpt-image-2.5-flare")
    assert cat.price(flare, {"resolution": "1K"}) == 3
    assert cat.default("image").key == "mage:gpt-image-2.5-flare"
    assert cat.default("video").key == "mage:lemon"


def test_vilva_credit_conversion():
    cfg = Config(bot_token="x", vilva_credits_per_credit=0.5, markup=2)
    cat = Catalog(cfg, [])
    from bot.catalog import ModelSpec

    spec = ModelSpec("vilva:m", "vilva", "image", "M", "m", base_units=3)
    assert cat.price(spec, {}) == 12


@pytest.mark.parametrize("prompt", ["nude girl", "ПОРНО", "раздень её", "teen model"])
def test_blocked_prompts(prompt):
    assert check_prompt(prompt) is not None


def test_allowed_prompt():
    assert check_prompt("кроссовки на белом фоне") is None
    assert check_prompt("  ") is not None
