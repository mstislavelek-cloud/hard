"""Сценарии бота через настоящий Dispatcher (mock-провайдер, без сети)."""
from __future__ import annotations

from aiogram.methods import AnswerPreCheckoutQuery, EditMessageText, SendInvoice, SendMessage

from bot.handlers import BTN_IMAGE, BTN_SETTINGS, BTN_VIDEO
from bot.providers.mock import MockProvider


def last_markup(h):
    for c in reversed(h.session.calls):
        if isinstance(c, (SendMessage, EditMessageText)) and c.reply_markup is not None:
            return c.reply_markup
    return None


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_start_shows_menu_and_text_makes_image(h):
    h.feed(h.msg("/start"), h.msg("кот в очках"))
    assert "SendPhoto" in h.session.names()
    price = h.app.catalog.price(h.app.catalog.get("mock:image"), {"aspect_ratio": "1:1"})
    assert price == 2 and h.db.balance(42) == 3 - price
    assert h.provider.calls[0][:2] == ("mock:image", "кот в очках")


def test_video_mode_not_enough_credits(h):
    h.feed(h.msg("/start"), h.msg(BTN_VIDEO), h.msg("волны"))
    assert h.provider.calls == []
    assert any("Не хватает кредитов" in t for t in h.session.texts())
    assert h.db.balance(42) == 3


def test_failed_generation_refunds(h):
    h.app.providers["mock"] = MockProvider(fail=True)
    h.feed(h.msg("/start"), h.msg("кот"))
    assert h.db.balance(42) == 3
    assert any("Кредиты вернул" in t for t in h.session.texts())


def test_blocked_prompt_not_charged(h):
    h.feed(h.msg("/start"), h.msg("nude"))
    assert h.provider.calls == [] and h.db.balance(42) == 3


def test_pick_model_and_advanced_params(h):
    h.db.ensure_user(42, "u", 500)
    h.feed(h.msg(BTN_SETTINGS), h.cb("sm:video"), h.cb("pm:video:0"), h.cb("adv"), h.cb("sp:video"))
    texts = [b.text for b in buttons(last_markup(h))]
    assert any(t.startswith("Длительность") for t in texts)
    # длительность 10 вместо 5 по умолчанию
    h.feed(h.cb("pp:video:duration"), h.cb("pv:video:duration:1"))
    assert h.db.get_settings(42)["params"]["mock:video"]["duration"] == "10"
    # seed
    h.feed(h.cb("ps:video"), h.msg("777"))
    h.feed(h.msg(BTN_VIDEO), h.msg("волны"))
    key, prompt, params, has_image = h.provider.calls[-1]
    assert key == "mock:video" and params == {"duration": "10", "seed": "777"}
    # цена удвоилась из-за длительности
    assert h.app.catalog.price(h.app.catalog.get("mock:video"), params) == 2 * h.app.catalog.price(
        h.app.catalog.get("mock:video"), {"duration": "5"})


def test_simple_mode_keeps_quick_params_ignores_advanced(h):
    h.db.ensure_user(42, "u", 500)
    h.db.save_settings(42, {"advanced": False, "kind": "video",
                            "params": {"mock:video": {"duration": "10", "seed": "7"}}})
    h.feed(h.msg("волны"))
    assert h.provider.calls[-1][2] == {"duration": "10"}   # длительность — быстрый параметр, seed — продвинутый


def test_kind_panel_has_model_and_quick_params(h):
    h.db.ensure_user(42, "u", 500)
    h.feed(h.msg(BTN_IMAGE))
    texts = [b.text for b in buttons(last_markup(h))]
    assert texts[0].startswith("🧠") and any(t.startswith("📐") for t in texts)
    # формат прямо из панели, без продвинутого режима
    h.feed(h.cb("pp:image:aspect_ratio:k"), h.cb("pv:image:aspect_ratio:1:k"))
    h.feed(h.msg("кот"))
    assert h.provider.calls[-1][2] == {"aspect_ratio": "9:16"}
    assert any(t.startswith("🧠") for t in (b.text for b in buttons(last_markup(h))))


def test_photo_with_caption_animates_in_video_mode(h):
    h.db.ensure_user(42, "u", 500)
    photo = [{"file_id": "f1", "file_unique_id": "u1", "width": 10, "height": 10}]
    h.feed(h.msg(BTN_VIDEO), h.msg(None, photo=photo, caption="оживи, лёгкий ветер"))
    key, prompt, params, has_image = h.provider.calls[-1]
    assert key == "mock:video" and has_image and prompt == "оживи, лёгкий ветер"


def test_reset_params(h):
    h.db.ensure_user(42, "u", 500)
    h.feed(h.cb("adv"), h.cb("pv:image:aspect_ratio:1"), h.cb("pr:image"))
    assert "mock:image" not in h.db.get_settings(42)["params"]


def test_buy_and_payment(h):
    pcq = {"pre_checkout_query": {"id": "p", "from": {"id": 42, "is_bot": False, "first_name": "U"},
                                  "currency": "XTR", "total_amount": 100, "invoice_payload": "pack:s"}}
    paid = h.msg(None, successful_payment={
        "currency": "XTR", "total_amount": 100, "invoice_payload": "pack:s",
        "telegram_payment_charge_id": "tg1", "provider_payment_charge_id": "p1"})
    h.feed(h.msg("/start"), h.cb("buy:s"), pcq, paid, paid)
    inv = [c for c in h.session.calls if isinstance(c, SendInvoice)][0]
    assert inv.currency == "XTR" and inv.prices[0].amount == 100
    assert [c for c in h.session.calls if isinstance(c, AnswerPreCheckoutQuery)][0].ok
    assert h.db.balance(42) == 23


def test_bad_pre_checkout_rejected(h):
    pcq = {"pre_checkout_query": {"id": "p", "from": {"id": 42, "is_bot": False, "first_name": "U"},
                                  "currency": "XTR", "total_amount": 1, "invoice_payload": "pack:s"}}
    h.feed(pcq)
    assert not [c for c in h.session.calls if isinstance(c, AnswerPreCheckoutQuery)][0].ok


def test_agent_unavailable_without_vilva(h):
    h.feed(h.msg("/agent сделай баннер"))
    assert any("Агент сейчас недоступен" in t for t in h.session.texts())


def test_admin_stats_only_for_admin(h):
    h.feed(h.msg("/stats", uid=42))
    assert not any("Пользователи" in t for t in h.session.texts())
    h.feed(h.msg("/stats", uid=1))
    assert any("Пользователи" in t for t in h.session.texts())


def test_admin_give_credits(h):
    h.feed(h.msg("/give 100", uid=1), h.msg("/give 50 42", uid=1), h.msg("/give -500 42", uid=1))
    assert h.db.balance(1) == 100
    assert h.db.balance(42) == 0
    assert any("Начислил 50" in t for t in h.session.texts())
    h.feed(h.msg("/give 1000", uid=42))  # не админ
    assert h.db.balance(42) == 0


def test_admin_prices_audit(h):
    h.feed(h.msg("/prices", uid=1))
    text = "\n".join(h.session.texts())
    assert "Курсы:" in text and "Тестовая картинка" in text and "→" in text
