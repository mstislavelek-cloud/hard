"""Обработчики команд, оплаты Stars и генерации."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    URLInputFile,
)

from .config import Config, Pack
from .db import Database, InsufficientCredits
from .moderation import check_prompt
from .providers import Media, ProviderError

log = logging.getLogger(__name__)

# Генерация не дольше этого времени, иначе кредиты возвращаются.
GENERATION_TIMEOUT = 660


def find_pack(cfg: Config, pack_id: str) -> Pack | None:
    return next((p for p in cfg.packs if p.id == pack_id), None)


def packs_keyboard(cfg: Config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"{p.title} — {p.stars} ⭐", callback_data=f"buy:{p.id}")]
            for p in cfg.packs
        ]
    )


def help_text(cfg: Config) -> str:
    return (
        "Генерирую картинки и видео по описанию.\n\n"
        f"• Просто напиши, что нарисовать — картинка, {cfg.image_cost} кр.\n"
        f"• /video описание — видео 5 сек, {cfg.video_cost} кр.\n"
        f"• Фото с подписью /video описание — оживлю фото, {cfg.video_cost} кр.\n\n"
        "/balance — баланс, /buy — пополнить, /terms — условия, /paysupport — помощь с оплатой"
    )


async def cmd_start(message: Message, db: Database, cfg: Config) -> None:
    user = message.from_user
    is_new = db.ensure_user(user.id, user.username, cfg.free_credits)
    greeting = f"Привет! Дарю {cfg.free_credits} бесплатных кредита.\n\n" if is_new else ""
    await message.answer(greeting + help_text(cfg))


async def cmd_help(message: Message, cfg: Config) -> None:
    await message.answer(help_text(cfg))


async def cmd_balance(message: Message, db: Database, cfg: Config) -> None:
    db.ensure_user(message.from_user.id, message.from_user.username, cfg.free_credits)
    await message.answer(f"Баланс: {db.balance(message.from_user.id)} кр.", reply_markup=packs_keyboard(cfg))


async def cmd_buy(message: Message, cfg: Config) -> None:
    await message.answer("Выбери пакет:", reply_markup=packs_keyboard(cfg))


async def cmd_terms(message: Message, cfg: Config) -> None:
    await message.answer(
        "Условия:\n"
        "• Кредиты покупаются за Telegram Stars и тратятся на генерации.\n"
        "• Если генерация не удалась, кредиты возвращаются автоматически.\n"
        "• Запрещено генерировать контент 18+, с несовершеннолетними, с реальными людьми без их согласия.\n"
        "• Возврат Stars за неизрасходованный пакет — через /paysupport в течение 14 дней."
    )


async def cmd_paysupport(message: Message, cfg: Config) -> None:
    await message.answer(
        f"Вопросы по оплате и возвратам: {cfg.support_contact}. "
        "Укажи дату покупки и пакет, ответим в течение 24 часов."
    )


async def cb_buy(callback: CallbackQuery, bot: Bot, cfg: Config) -> None:
    pack = find_pack(cfg, callback.data.split(":", 1)[1])
    if pack is None:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=pack.title,
        description=f"{pack.credits} кредитов для генерации картинок и видео",
        payload=f"pack:{pack.id}",
        currency="XTR",
        prices=[LabeledPrice(label=pack.title, amount=pack.stars)],
    )
    await callback.answer()


async def pre_checkout(query: PreCheckoutQuery, cfg: Config) -> None:
    pack_id = query.invoice_payload.removeprefix("pack:")
    pack = find_pack(cfg, pack_id)
    if pack is None or query.currency != "XTR" or query.total_amount != pack.stars:
        await query.answer(ok=False, error_message="Пакет устарел, открой /buy заново")
        return
    await query.answer(ok=True)


async def on_payment(message: Message, db: Database, cfg: Config) -> None:
    sp = message.successful_payment
    pack = find_pack(cfg, sp.invoice_payload.removeprefix("pack:"))
    if pack is None:
        log.error("Оплата неизвестного пакета: %s", sp.invoice_payload)
        await message.answer(f"Оплата получена, но пакет не найден. Напиши в {cfg.support_contact}")
        return
    db.ensure_user(message.from_user.id, message.from_user.username, cfg.free_credits)
    credited = db.add_payment(sp.telegram_payment_charge_id, message.from_user.id, sp.total_amount, pack.credits)
    if credited:
        await message.answer(
            f"Готово! +{pack.credits} кр. Баланс: {db.balance(message.from_user.id)} кр."
        )


async def cmd_stats(message: Message, db: Database, cfg: Config) -> None:
    if message.from_user.id not in cfg.admin_ids:
        return
    s = db.stats()
    await message.answer(
        f"Пользователи: {s['users']}\nПлатящие: {s['payers']}\nStars: {s['stars']}\n"
        f"Картинки: {s['images']}, видео: {s['videos']}, ошибки: {s['failed']}"
    )


async def cmd_refund(message: Message, command: CommandObject, bot: Bot, db: Database, cfg: Config) -> None:
    if message.from_user.id not in cfg.admin_ids:
        return
    charge_id = (command.args or "").strip()
    payment = db.get_payment(charge_id)
    if payment is None:
        await message.answer("Платёж не найден")
        return
    if payment[3]:
        await message.answer("Уже возвращён")
        return
    await bot.refund_star_payment(user_id=payment[0], telegram_payment_charge_id=charge_id)
    db.mark_refunded(charge_id)
    await message.answer(f"Вернул {payment[1]} ⭐ пользователю {payment[0]}")


async def cmd_video_from_photo(message: Message, command: CommandObject, bot: Bot, **deps) -> None:
    photo = message.photo[-1]
    buf = await bot.download(photo.file_id)
    await generate(message, "video", command.args or "", image=buf.read(), **deps)


async def cmd_video(message: Message, command: CommandObject, **deps) -> None:
    await generate(message, "video", command.args or "", **deps)


async def cmd_img(message: Message, command: CommandObject, **deps) -> None:
    await generate(message, "image", command.args or "", **deps)


async def on_text(message: Message, **deps) -> None:
    await generate(message, "image", message.text, **deps)


async def generate(
    message: Message,
    kind: str,
    prompt: str,
    *,
    db: Database,
    cfg: Config,
    image_provider,
    video_provider,
    busy: set[int],
    image: bytes | None = None,
    **_,
) -> None:
    user = message.from_user
    db.ensure_user(user.id, user.username, cfg.free_credits)
    reason = check_prompt(prompt)
    if reason:
        await message.answer(reason)
        return
    if user.id in busy:
        await message.answer("Подожди, предыдущая генерация ещё идёт")
        return
    cost = cfg.video_cost if kind == "video" else cfg.image_cost
    try:
        gen_id = db.charge(user.id, kind, prompt, cost)
    except InsufficientCredits:
        await message.answer(
            f"Не хватает кредитов: нужно {cost}, у тебя {db.balance(user.id)}.",
            reply_markup=packs_keyboard(cfg),
        )
        return

    busy.add(user.id)
    status = await message.answer("Генерирую… видео может занять пару минут" if kind == "video" else "Генерирую…")
    ok = False
    try:
        if kind == "video":
            media = await asyncio.wait_for(video_provider.generate_video(prompt, image), GENERATION_TIMEOUT)
        else:
            media = await asyncio.wait_for(image_provider.generate_image(prompt), GENERATION_TIMEOUT)
        await send_media(message, kind, media)
        ok = True
    except (ProviderError, asyncio.TimeoutError) as e:
        log.warning("Генерация %s не удалась: %s", gen_id, e)
    except Exception:
        log.exception("Ошибка генерации %s", gen_id)
    finally:
        db.finish(gen_id, ok)
        busy.discard(user.id)
        try:
            await status.delete()
        except Exception:
            pass
    if not ok:
        await message.answer("Не получилось сгенерировать, кредиты вернул. Попробуй переформулировать запрос")


async def send_media(message: Message, kind: str, media: Media) -> None:
    if media.data is not None:
        file = BufferedInputFile(media.data, filename=media.filename)
    elif media.url:
        file = URLInputFile(media.url, filename=media.filename)
    else:
        raise ProviderError("пустой результат")
    if kind == "video" and media.filename.endswith(".mp4"):
        await message.answer_video(file)
    elif kind == "image":
        await message.answer_photo(file)
    else:
        await message.answer_document(file)


def create_router() -> Router:
    """Новый Router на каждый Dispatcher (Router нельзя подключить дважды)."""
    r = Router()
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_balance, Command("balance"))
    r.message.register(cmd_buy, Command("buy"))
    r.message.register(cmd_terms, Command("terms"))
    r.message.register(cmd_paysupport, Command("paysupport"))
    r.callback_query.register(cb_buy, F.data.startswith("buy:"))
    r.pre_checkout_query.register(pre_checkout)
    r.message.register(on_payment, F.successful_payment)
    r.message.register(cmd_stats, Command("stats"))
    r.message.register(cmd_refund, Command("refund"))
    r.message.register(cmd_video_from_photo, Command("video"), F.photo)
    r.message.register(cmd_video, Command("video"))
    r.message.register(cmd_img, Command("img"))
    r.message.register(on_text, F.text & ~F.text.startswith("/"))
    return r
