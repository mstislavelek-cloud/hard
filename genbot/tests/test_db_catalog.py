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

# Цены Mage, снятые через их estimate_cost (gems): (модель, параметры, ожидаемые gems)
MAGE_ESTIMATES = [
    ("mage:gpt-image-2.5-flare", {"resolution": "1K", "quality": "low"}, 9),
    ("mage:gpt-image-2.5-flare", {"resolution": "1K", "quality": "medium"}, 80),
    ("mage:gpt-image-2.5-flare", {"resolution": "1K", "quality": "high"}, 317),
    ("mage:gpt-image-2.5-flare", {"resolution": "2K", "quality": "medium"}, 160),
    ("mage:gpt-image-2.5-flare", {"resolution": "2K", "quality": "high"}, 634),
    ("mage:guava-2", {"resolution": "2K"}, 36),
    ("mage:guava-2-pro", {"resolution": "1K"}, 48),
    ("mage:guava-2-pro", {"resolution": "2K"}, 90),
    ("mage:mango-v3", {"resolution": "1K"}, 68),
    ("mage:mango-v3", {"resolution": "2K"}, 135),
    ("mage:mango-v3s", {"resolution": "3K"}, 55),
    ("mage:mango-v2", {"resolution": "4K"}, 60),
    ("mage:lemon", {"resolution": "480p", "duration": "3"}, 245),
    ("mage:lemon", {"resolution": "480p", "duration": "5"}, 408),
    ("mage:lemon", {"resolution": "480p", "duration": "10"}, 816),
    ("mage:lemon", {"resolution": "720p", "duration": "5"}, 840),
    ("mage:lemon", {"resolution": "1080p", "duration": "5"}, 1680),
    ("mage:cherry-mini", {"resolution": "480p", "duration": "10"}, 600),
    ("mage:cherry-mini", {"resolution": "720p", "duration": "5"}, 600),
    ("mage:cherry", {"resolution": "720p", "duration": "5"}, 900),
    ("mage:cherry-pro", {"resolution": "1080p", "duration": "5"}, 2775),
    ("mage:cherry-pro", {"resolution": "4k", "duration": "5"}, 5850),
    ("mage:cherry-2-pro", {"resolution": "480p", "duration": "5"}, 773),
    ("mage:cherry-2-pro", {"resolution": "720p", "duration": "10"}, 3465),
    ("mage:cherry-2-pro", {"resolution": "1080p", "duration": "10"}, 8535),
    ("mage:gpt-image-2", {"resolution": "2K", "quality": "high"}, 634),
    ("mage:gpt-image-2.5-sunburst", {"resolution": "1K", "quality": "medium"}, 80),
    ("mage:nano-banana-v2", {"resolution": "512"}, 68),
    ("mage:nano-banana-v2", {"resolution": "1K"}, 101),
    ("mage:nano-banana-v2", {"resolution": "4K"}, 227),
    ("mage:mango-v3-turbo", {"resolution": "2K"}, 27),
    ("mage:mango", {"resolution": "4K"}, 45),
    ("mage:guava-pro-v1-5", {"resolution": "2K"}, 79),
    ("mage:grok-imagine-image", {"resolution": "2k"}, 30),
    ("mage:grok-imagine-image-quality", {"resolution": "2k"}, 105),
    ("mage:grok-imagine-image-2.0", {"resolution": "2k"}, 120),
    ("mage:z-image-turbo", {"resolution": "2k"}, 10),
    ("mage:flux2-dev", {"resolution": "2k"}, 40),
    ("mage:krea-2-turbo", {"resolution": "2k"}, 20),
    ("mage:anima-v1", {"resolution": "2k"}, 20),
    ("mage:chroma-v1-hd", {}, 35),
    ("mage:hidream-fast", {}, 20),
    ("mage:sd-3-5-large", {}, 10),
    ("mage:berry-2", {"resolution": "480p", "duration": "10"}, 630),
    ("mage:berry-2", {"resolution": "1080p", "duration": "5"}, 810),
    ("mage:berry", {"resolution": "1080p", "duration": "5"}, 1260),
    ("mage:blueberry-v2", {"resolution": "720p", "duration": "10"}, 900),
    ("mage:blueberry", {"resolution": "1080p", "duration": "5"}, 795),
    ("mage:raspberry", {"resolution": "1080p", "duration": "5"}, 675),
    ("mage:kiwi", {"resolution": "480p", "duration": "10"}, 540),
    ("mage:kiwi", {"resolution": "1080p", "duration": "5"}, 795),
    ("mage:grok-imagine-video", {"resolution": "720p", "duration": "5"}, 525),
    ("mage:melon", {"resolution": "540p", "duration": "10"}, 368),
    ("mage:melon", {"resolution": "1080p", "duration": "5"}, 342),
    ("mage:melon-pro", {"resolution": "720p", "duration": "5"}, 525),
    ("mage:minimax-h3-turbo", {"resolution": "768p", "duration": "5"}, 75),
    ("mage:minimax-h3", {"resolution": "720p", "duration": "5"}, 270),
    ("mage:plum", {"resolution": "768P", "duration": "10"}, 1080),
    ("mage:plum", {"resolution": "2K", "duration": "5"}, 780),
    ("mage:plum-max", {"resolution": "480P", "duration": "10"}, 675),
    ("mage:plum-max", {"resolution": "768P", "duration": "5"}, 540),
    ("mage:wan22-video", {"resolution": "720p"}, 495),
    ("mage:wan22-video-lightning", {"resolution": "360p"}, 75),
    ("mage:ltx-video-096-distilled", {"resolution": "720p"}, 25),
]


@pytest.mark.parametrize("key,params,gems", MAGE_ESTIMATES)
def test_mage_prices_match_estimate_cost(key, params, gems):
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    assert cat.estimate_units(cat.get(key), params) == gems


def test_mage_credit_prices():
    # 10 000 gems = $10, 1 кр бота ≈ $0.0665, наценка ×2
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    flare = cat.get("mage:gpt-image-2.5-flare")
    assert cat.price(flare, {"resolution": "1K", "quality": "low"}) == 1
    assert cat.price(flare, {"resolution": "2K", "quality": "high"}) == 20   # 634 gems = $0.634
    lemon = cat.get("mage:lemon")
    assert cat.price(lemon, {"resolution": "480p", "duration": "5"}) == 13
    assert cat.price(lemon, {"resolution": "1080p", "duration": "5"}) == 51
    assert cat.default("image").key == "mage:gpt-image-2.5-flare"
    assert cat.default("video").key == "mage:lemon"


def test_vilva_credit_conversion():
    # реальные курсы: 4 000 кредитов Vilva = $21
    cat = Catalog(Config(bot_token="x"), [])
    from bot.catalog import ModelSpec

    gpt = ModelSpec("vilva:gpt", "vilva", "image", "GPT Image 2.5", "gpt", base_units=9)
    assert cat.price(gpt, {}) == 2   # 9 × $0.00525 = $0.047 → ×2 / 0.0665 = 1.42
    mage_gpt = ModelSpec("mage:gpt", "mage", "image", "GPT Image 2.5", "gpt", base_units=9)
    assert cat.price(mage_gpt, {}) == 1   # 9 gems = $0.009 — у Mage та же модель дешевле


@pytest.mark.parametrize("prompt", ["nude girl", "ПОРНО", "раздень её", "teen model"])
def test_blocked_prompts(prompt):
    assert check_prompt(prompt) is not None


def test_allowed_prompt():
    assert check_prompt("кроссовки на белом фоне") is None
    assert check_prompt("  ") is not None


# доплата mage за фото-референс, снято через estimate_cost с image_url
MAGE_IMAGE_ESTIMATES = [
    ("mage:gpt-image-2.5-flare", {"resolution": "1K", "quality": "low"}, 24),
    ("mage:gpt-image-2.5-flare", {"resolution": "1K", "quality": "high"}, 332),
    ("mage:gpt-image-2.5-flare", {"resolution": "2K", "quality": "low"}, 33),
    ("mage:guava-2-pro", {"resolution": "1K"}, 53),
    ("mage:lemon", {"resolution": "480p", "duration": "5"}, None),  # без доплаты
]


@pytest.mark.parametrize("key,params,gems", MAGE_IMAGE_ESTIMATES)
def test_mage_reference_surcharge(key, params, gems):
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    spec = cat.get(key)
    expected = gems if gems is not None else cat.estimate_units(spec, params)
    assert cat.estimate_units(spec, params, with_image=True) == expected


def test_mage_reference_multiplier_not_cheaper():
    # wan / ltx: с фото ≈ ×1.2, цена не должна быть ниже замера
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    assert cat.estimate_units(cat.get("mage:wan22-video"), {"resolution": "480p"}, with_image=True) >= 195
    assert cat.estimate_units(cat.get("mage:wan22-video"), {"resolution": "720p"}, with_image=True) >= 595


# продвинутые параметры mage, влияющие на цену (estimate_cost 2026-10-02)
MAGE_PARAM_ESTIMATES = [
    ("mage:nano-banana-v2", {"resolution": "1K", "web_search": "true"}, 121),
    ("mage:nano-banana-v2", {"resolution": "1K", "web_search": "true", "image_search": "true"}, 141),
    ("mage:nano-banana-v2", {"resolution": "1K", "thinking_level": "high"}, 101),
    ("mage:melon", {"resolution": "720p", "duration": "10", "generate_audio": "true"}, 657),
    ("mage:flux2-dev", {"resolution": "1k", "num_inference_steps": "20"}, 30),
    ("mage:flux2-dev", {"resolution": "2k", "num_inference_steps": "50"}, 60),
    ("mage:chroma-v1-hd", {"num_inference_steps": "80"}, 55),
    ("mage:hidream-fast", {"num_inference_steps": "32"}, 30),
    ("mage:sdxl-plus", {"num_inference_steps": "100"}, 20),
]


@pytest.mark.parametrize("key,params,gems", MAGE_PARAM_ESTIMATES)
def test_mage_param_prices(key, params, gems):
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    assert cat.estimate_units(cat.get(key), params) == gems


def test_mage_steps_multiplier_not_cheaper():
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    wan = cat.get("mage:wan22-video")
    assert cat.estimate_units(wan, {"resolution": "720p", "num_inference_steps": "30"}) >= 985
    ltx = cat.get("mage:ltx-video-096-distilled")
    for res, gems in (("240p", 15), ("480p", 25), ("720p", 45)):
        assert cat.estimate_units(ltx, {"resolution": res, "num_inference_steps": "16"}) >= gems


def test_mage_advanced_options_exposed():
    cat = Catalog(Config(bot_token="x", mage_key="k"), list(MAGE_MODELS))
    assert "negative_prompt" in cat.get("mage:sdxl").text_params
    assert {"web_search", "image_search", "thinking_level"} <= set(cat.get("mage:nano-banana-v2").options)
    assert "prompt_extend" in cat.get("mage:guava-2-pro").options
    lo, hi = cat.price_range(cat.get("mage:melon"))
    assert hi > cat.price(cat.get("mage:melon"), {"resolution": "1080p", "duration": "16"})
