"""Обработчики: меню, выбор моделей, продвинутые параметры, генерация, агент Vilva, оплата Stars."""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    URLInputFile,
)

from .catalog import Catalog, ModelSpec
from .config import Config, Pack
from .db import Database, InsufficientCredits
from .moderation import check_prompt
from .providers import Media, ProviderError

log = logging.getLogger(__name__)

GENERATION_TIMEOUT = 960
AGENT_POLL = 6.0
AGENT_TIMEOUT = 45 * 60

BTN_IMAGE = "🖼 Картинка"
BTN_VIDEO = "🎬 Видео"
BTN_AGENT = "🤖 Агент"
BTN_SETTINGS = "⚙️ Настройки"
BTN_BALANCE = "💰 Баланс"
MENU_BUTTONS = {BTN_IMAGE, BTN_VIDEO, BTN_AGENT, BTN_SETTINGS, BTN_BALANCE}

PARAM_LABELS = {
    "aspect_ratio": "Формат",
    "resolution": "Разрешение",
    "duration": "Длительность, сек",
    "audio": "Звук",
}
VALUE_LABELS = {"true": "вкл", "false": "выкл"}
KIND_LABEL = {"image": "картинок", "video": "видео"}


@dataclass
class App:
    """Всё, что нужно обработчикам: настройки, база, каталог, провайдеры и состояние."""

    cfg: Config
    db: Database
    catalog: Catalog
    providers: dict
    busy: set[int] = field(default_factory=set)
    # Ожидаемый ввод: user_id → ("agent_brief",) | ("agent_answer", gen_id) | ("seed", kind)
    pending: dict[int, tuple] = field(default_factory=dict)
    agent_drafts: dict[int, dict] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    agent_poll: float = AGENT_POLL

    @property
    def vilva(self):
        return self.providers.get("vilva")

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


# ---------- настройки пользователя ----------

def settings_of(app: App, user_id: int) -> dict:
    s = app.db.get_settings(user_id)
    s.setdefault("kind", "image")
    s.setdefault("advanced", False)
    s.setdefault("params", {})
    return s


def model_of(app: App, settings: dict, kind: str) -> ModelSpec | None:
    return app.catalog.get(settings.get(f"{kind}_model")) or app.catalog.default(kind)


def params_of(app: App, settings: dict, spec: ModelSpec) -> dict[str, str]:
    params = {k: v for k, v in spec.simple.items() if k in spec.options}
    if settings.get("advanced"):
        for k, v in settings["params"].get(spec.key, {}).items():
            if k == "seed" or v in spec.options.get(k, ()):
                params[k] = v
    return params


def describe_params(spec: ModelSpec, params: dict[str, str]) -> str:
    parts = [f"{PARAM_LABELS.get(k, k)}: {VALUE_LABELS.get(v, v)}" for k, v in params.items() if k != "seed"]
    if "seed" in params:
        parts.append(f"seed: {params['seed']}")
    return ", ".join(parts)


# ---------- клавиатуры ----------

def main_menu(app: App) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_IMAGE), KeyboardButton(text=BTN_VIDEO)]]
    second = [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_BALANCE)]
    if app.vilva:
        second.insert(0, KeyboardButton(text=BTN_AGENT))
    rows.append(second)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def packs_keyboard(cfg: Config) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{p.title} — {p.stars} ⭐", callback_data=f"buy:{p.id}")] for p in cfg.packs
    ])


def settings_view(app: App, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    rows = []
    lines = ["⚙️ Настройки\n"]
    for kind, icon in (("image", "🖼"), ("video", "🎬")):
        spec = model_of(app, s, kind)
        if spec is None:
            continue
        params = params_of(app, s, spec)
        lines.append(f"{icon} {spec.title} — ~{app.catalog.price(spec, params)} кр\n   {describe_params(spec, params)}")
        rows.append([InlineKeyboardButton(text=f"{icon} Модель {KIND_LABEL[kind]}: {spec.title}", callback_data=f"sm:{kind}")])
    rows.append([InlineKeyboardButton(
        text=f"🧪 Продвинутый режим: {'вкл' if s['advanced'] else 'выкл'}", callback_data="adv")])
    if s["advanced"]:
        rows.append([
            InlineKeyboardButton(text="🔧 Параметры картинок", callback_data="sp:image"),
            InlineKeyboardButton(text="🔧 Параметры видео", callback_data="sp:video"),
        ])
    else:
        lines.append("\nВ продвинутом режиме можно выбрать формат, разрешение, длительность, звук и seed")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def models_view(app: App, user_id: int, kind: str) -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    current = model_of(app, s, kind)
    models = app.catalog.by_kind(kind)
    lines = [f"Выбери модель {KIND_LABEL[kind]} (цена при базовых параметрах):\n"]
    rows = []
    for i, m in enumerate(models):
        price = app.catalog.price(m, {k: v for k, v in m.simple.items() if k in m.options})
        mark = "✅ " if current and m.key == current.key else ""
        src = "Mage" if m.provider == "mage" else "Vilva" if m.provider == "vilva" else "тест"
        lines.append(f"• {m.title} ({src}) — ~{price} кр. {m.note}")
        rows.append([InlineKeyboardButton(text=f"{mark}{m.title} · ~{price} кр", callback_data=f"pm:{kind}:{i}")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data="settings")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def params_view(app: App, user_id: int, kind: str) -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    spec = model_of(app, s, kind)
    params = params_of(app, s, spec)
    rows = []
    for name in spec.options:
        value = params.get(name, "по умолчанию")
        rows.append([InlineKeyboardButton(
            text=f"{PARAM_LABELS.get(name, name)}: {VALUE_LABELS.get(value, value)}", callback_data=f"pp:{kind}:{name}")])
    rows.append([InlineKeyboardButton(text=f"🎲 Seed: {params.get('seed', 'случайный')}", callback_data=f"ps:{kind}")])
    rows.append([
        InlineKeyboardButton(text="↩️ Сбросить", callback_data=f"pr:{kind}"),
        InlineKeyboardButton(text="« Назад", callback_data="settings"),
    ])
    text = (f"🔧 {spec.title}\nТекущие: {describe_params(spec, params)}\n"
            f"Цена с этими параметрами: ~{app.catalog.price(spec, params)} кр")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def values_view(app: App, user_id: int, kind: str, name: str) -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    spec = model_of(app, s, kind)
    current = params_of(app, s, spec).get(name)
    rows, row = [], []
    for i, v in enumerate(spec.options.get(name, ())):
        price = app.catalog.price(spec, {**params_of(app, s, spec), name: v})
        label = f"{'✅ ' if v == current else ''}{VALUE_LABELS.get(v, v)} · ~{price}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"pv:{kind}:{name}:{i}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"sp:{kind}")])
    return f"{PARAM_LABELS.get(name, name)} для {spec.title} (цена в кредитах):", InlineKeyboardMarkup(inline_keyboard=rows)


def help_text(app: App) -> str:
    text = (
        "Генерирую картинки и видео лучшими моделями.\n\n"
        f"{BTN_IMAGE} / {BTN_VIDEO} — выбрать, что генерировать, потом просто пиши описание\n"
        "📎 Фото с подписью — правка картинки или оживление фото (в режиме видео)\n"
        f"{BTN_SETTINGS} — модели и продвинутый режим со всеми параметрами\n"
    )
    if app.vilva:
        text += f"{BTN_AGENT} — креативный агент: опиши задачу, он спланирует и сделает проект целиком\n"
    return text + "\n/balance — баланс, /buy — пополнить, /terms — условия, /paysupport — помощь с оплатой"


async def safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except Exception:
        await callback.message.answer(text, reply_markup=markup)


# ---------- базовые команды ----------

async def cmd_start(message: Message, app: App) -> None:
    user = message.from_user
    is_new = app.db.ensure_user(user.id, user.username, app.cfg.free_credits)
    greeting = f"Привет! Дарю {app.cfg.free_credits} бесплатных кредита.\n\n" if is_new else ""
    await message.answer(greeting + help_text(app), reply_markup=main_menu(app))


async def cmd_help(message: Message, app: App) -> None:
    await message.answer(help_text(app), reply_markup=main_menu(app))


async def cmd_balance(message: Message, app: App) -> None:
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    await message.answer(f"Баланс: {app.db.balance(message.from_user.id)} кр.", reply_markup=packs_keyboard(app.cfg))


async def cmd_buy(message: Message, app: App) -> None:
    await message.answer("Выбери пакет:", reply_markup=packs_keyboard(app.cfg))


async def cmd_terms(message: Message, app: App) -> None:
    await message.answer(
        "Условия:\n"
        "• Кредиты покупаются за Telegram Stars и тратятся на генерации и работу агента.\n"
        "• Цена генерации показывается заранее; если генерация не удалась, кредиты возвращаются.\n"
        "• Для агента резервируется бюджет, неизрасходованная часть возвращается.\n"
        "• Запрещено генерировать контент 18+, с несовершеннолетними, с реальными людьми без их согласия.\n"
        "• Возврат Stars за неизрасходованный пакет — через /paysupport в течение 14 дней."
    )


async def cmd_paysupport(message: Message, app: App) -> None:
    await message.answer(
        f"Вопросы по оплате и возвратам: {app.cfg.support_contact}. "
        "Укажи дату покупки и пакет, ответим в течение 24 часов."
    )


async def cmd_settings(message: Message, app: App) -> None:
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    text, kb = settings_view(app, message.from_user.id)
    await message.answer(text, reply_markup=kb)


async def btn_kind(message: Message, app: App) -> None:
    user = message.from_user
    app.db.ensure_user(user.id, user.username, app.cfg.free_credits)
    kind = "image" if message.text == BTN_IMAGE else "video"
    s = settings_of(app, user.id)
    s["kind"] = kind
    app.db.save_settings(user.id, s)
    spec = model_of(app, s, kind)
    if spec is None:
        await message.answer("Модели этого типа сейчас недоступны")
        return
    params = params_of(app, s, spec)
    hint = ("Опиши картинку, например: «кроссовки на белом фоне, студийный свет». "
            "Можно прислать фото с подписью — изменю его")
    if kind == "video":
        hint = ("Опиши видео, например: «волны разбиваются о скалы на закате, медленный пролёт камеры». "
                "Можно прислать фото с подписью — оживлю его")
    await message.answer(
        f"Режим: {'картинки' if kind == 'image' else 'видео'}\nМодель: {spec.title} — ~{app.catalog.price(spec, params)} кр\n"
        f"Параметры: {describe_params(spec, params)}\n\n{hint}"
    )


# ---------- настройки: колбэки ----------

async def cb_settings(callback: CallbackQuery, app: App) -> None:
    text, kb = settings_view(app, callback.from_user.id)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_models(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    text, kb = models_view(app, callback.from_user.id, kind)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_pick_model(callback: CallbackQuery, app: App) -> None:
    _, kind, idx = callback.data.split(":")
    models = app.catalog.by_kind(kind)
    if not idx.isdigit() or int(idx) >= len(models):
        await callback.answer("Модель не найдена", show_alert=True)
        return
    uid = callback.from_user.id
    app.db.ensure_user(uid, callback.from_user.username, app.cfg.free_credits)
    s = settings_of(app, uid)
    s[f"{kind}_model"] = models[int(idx)].key
    app.db.save_settings(uid, s)
    text, kb = settings_view(app, uid)
    await safe_edit(callback, text, kb)
    await callback.answer(f"Выбрано: {models[int(idx)].title}")


async def cb_toggle_advanced(callback: CallbackQuery, app: App) -> None:
    uid = callback.from_user.id
    app.db.ensure_user(uid, callback.from_user.username, app.cfg.free_credits)
    s = settings_of(app, uid)
    s["advanced"] = not s["advanced"]
    app.db.save_settings(uid, s)
    text, kb = settings_view(app, uid)
    await safe_edit(callback, text, kb)
    await callback.answer("Продвинутый режим включён" if s["advanced"] else "Продвинутый режим выключен")


async def cb_params(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    if model_of(app, settings_of(app, callback.from_user.id), kind) is None:
        await callback.answer("Нет моделей", show_alert=True)
        return
    text, kb = params_view(app, callback.from_user.id, kind)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_param_values(callback: CallbackQuery, app: App) -> None:
    _, kind, name = callback.data.split(":", 2)
    text, kb = values_view(app, callback.from_user.id, kind, name)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_set_value(callback: CallbackQuery, app: App) -> None:
    _, kind, name, idx = callback.data.split(":")
    uid = callback.from_user.id
    s = settings_of(app, uid)
    spec = model_of(app, s, kind)
    values = spec.options.get(name, ())
    if not idx.isdigit() or int(idx) >= len(values):
        await callback.answer("Значение не найдено", show_alert=True)
        return
    s["params"].setdefault(spec.key, {})[name] = values[int(idx)]
    app.db.save_settings(uid, s)
    text, kb = params_view(app, uid, kind)
    await safe_edit(callback, text, kb)
    await callback.answer("Сохранено")


async def cb_seed(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    app.pending[callback.from_user.id] = ("seed", kind)
    await callback.message.answer("Пришли seed числом (одинаковый seed даёт повторяемый результат) или 0 — случайный")
    await callback.answer()


async def cb_reset_params(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    uid = callback.from_user.id
    s = settings_of(app, uid)
    spec = model_of(app, s, kind)
    s["params"].pop(spec.key, None)
    app.db.save_settings(uid, s)
    text, kb = params_view(app, uid, kind)
    await safe_edit(callback, text, kb)
    await callback.answer("Сброшено")


# ---------- оплата Stars ----------

def find_pack(cfg: Config, pack_id: str) -> Pack | None:
    return next((p for p in cfg.packs if p.id == pack_id), None)


async def cb_buy(callback: CallbackQuery, bot: Bot, app: App) -> None:
    pack = find_pack(app.cfg, callback.data.split(":", 1)[1])
    if pack is None:
        await callback.answer("Пакет не найден", show_alert=True)
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=pack.title,
        description=f"{pack.credits} кредитов для генерации картинок, видео и работы агента",
        payload=f"pack:{pack.id}",
        currency="XTR",
        prices=[LabeledPrice(label=pack.title, amount=pack.stars)],
    )
    await callback.answer()


async def pre_checkout(query: PreCheckoutQuery, app: App) -> None:
    pack = find_pack(app.cfg, query.invoice_payload.removeprefix("pack:"))
    if pack is None or query.currency != "XTR" or query.total_amount != pack.stars:
        await query.answer(ok=False, error_message="Пакет устарел, открой /buy заново")
        return
    await query.answer(ok=True)


async def on_payment(message: Message, app: App) -> None:
    sp = message.successful_payment
    pack = find_pack(app.cfg, sp.invoice_payload.removeprefix("pack:"))
    if pack is None:
        log.error("Оплата неизвестного пакета: %s", sp.invoice_payload)
        await message.answer(f"Оплата получена, но пакет не найден. Напиши в {app.cfg.support_contact}")
        return
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    if app.db.add_payment(sp.telegram_payment_charge_id, message.from_user.id, sp.total_amount, pack.credits):
        await message.answer(f"Готово! +{pack.credits} кр. Баланс: {app.db.balance(message.from_user.id)} кр.")


async def cmd_stats(message: Message, app: App) -> None:
    if message.from_user.id not in app.cfg.admin_ids:
        return
    s = app.db.stats()
    await message.answer(
        f"Пользователи: {s['users']}\nПлатящие: {s['payers']}\nStars: {s['stars']}\n"
        f"Картинки: {s['images']}, видео: {s['videos']}, агент: {s['agent']}, ошибки: {s['failed']}"
    )


async def cmd_refund(message: Message, command: CommandObject, bot: Bot, app: App) -> None:
    if message.from_user.id not in app.cfg.admin_ids:
        return
    charge_id = (command.args or "").strip()
    payment = app.db.get_payment(charge_id)
    if payment is None:
        await message.answer("Платёж не найден")
        return
    if payment[3]:
        await message.answer("Уже возвращён")
        return
    await bot.refund_star_payment(user_id=payment[0], telegram_payment_charge_id=charge_id)
    app.db.mark_refunded(charge_id)
    await message.answer(f"Вернул {payment[1]} ⭐ пользователю {payment[0]}")


# ---------- генерация ----------

async def cmd_img(message: Message, command: CommandObject, app: App, bot: Bot) -> None:
    await generate(app, bot, message, "image", command.args or "")


async def cmd_video(message: Message, command: CommandObject, app: App, bot: Bot) -> None:
    await generate(app, bot, message, "video", command.args or "")


async def on_photo(message: Message, app: App, bot: Bot) -> None:
    caption = (message.caption or "").strip()
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    kind = settings_of(app, message.from_user.id).get("kind", "image")
    for cmd, k in (("/video", "video"), ("/img", "image")):
        if caption.startswith(cmd):
            kind, caption = k, caption[len(cmd):].strip()
    buf = await bot.download(message.photo[-1].file_id)
    await generate(app, bot, message, kind, caption, image=buf.read())


async def on_text(message: Message, app: App, bot: Bot) -> None:
    uid = message.from_user.id
    state = app.pending.pop(uid, None)
    if state and state[0] == "seed":
        await set_seed(message, app, state[1])
        return
    if state and state[0] == "agent_brief":
        await agent_choose_mode(message, app, message.text)
        return
    if state and state[0] == "agent_answer":
        await agent_send_answer(message, app, state[1])
        return
    app.db.ensure_user(uid, message.from_user.username, app.cfg.free_credits)
    await generate(app, bot, message, settings_of(app, uid).get("kind", "image"), message.text)


async def set_seed(message: Message, app: App, kind: str) -> None:
    text = message.text.strip()
    if not text.lstrip("-").isdigit():
        await message.answer("Нужно целое число, например 42, или 0 — случайный")
        return
    uid = message.from_user.id
    s = settings_of(app, uid)
    spec = model_of(app, s, kind)
    p = s["params"].setdefault(spec.key, {})
    if int(text) == 0:
        p.pop("seed", None)
    else:
        p["seed"] = str(int(text))
    app.db.save_settings(uid, s)
    t, kb = params_view(app, uid, kind)
    await message.answer(t, reply_markup=kb)


async def notify_admins(app: App, bot: Bot, text: str) -> None:
    for admin in app.cfg.admin_ids:
        try:
            await bot.send_message(admin, text)
        except Exception:
            log.exception("Не удалось уведомить админа %s", admin)


async def generate(app: App, bot: Bot, message: Message, kind: str, prompt: str, image: bytes | None = None) -> None:
    user = message.from_user
    app.db.ensure_user(user.id, user.username, app.cfg.free_credits)
    reason = check_prompt(prompt)
    if reason:
        await message.answer(reason)
        return
    s = settings_of(app, user.id)
    spec = model_of(app, s, kind)
    if spec is None:
        await message.answer("Модели этого типа сейчас недоступны")
        return
    if image is not None and not spec.image_field:
        await message.answer(f"{spec.title} не работает с фото. Выбери другую модель в {BTN_SETTINGS}")
        return
    provider = app.providers.get(spec.provider)
    if provider is None:
        await message.answer("Эта модель сейчас недоступна, выбери другую в настройках")
        return
    if user.id in app.busy:
        await message.answer("Подожди, предыдущая генерация ещё идёт")
        return
    params = params_of(app, s, spec)
    cost = app.catalog.price(spec, params)
    try:
        gen_id = app.db.charge(user.id, kind, prompt, cost)
    except InsufficientCredits:
        await message.answer(
            f"Не хватает кредитов: нужно ~{cost}, у тебя {app.db.balance(user.id)}.",
            reply_markup=packs_keyboard(app.cfg),
        )
        return

    app.busy.add(user.id)
    wait = "пару минут" if kind == "video" else "несколько секунд"
    status = await message.answer(f"Генерирую на {spec.title} (~{cost} кр), это займёт {wait}…")
    ok, final, error = False, cost, None
    try:
        result = await asyncio.wait_for(provider.generate(spec, prompt, params, image), GENERATION_TIMEOUT)
        await send_media(message, kind, result.media)
        ok = True
        actual = app.catalog.credits(spec, result.units) if result.units else None
        final = app.db.finish(gen_id, True, actual)
    except ProviderError as e:
        error = e
        log.warning("Генерация %s не удалась: %s", gen_id, e)
    except asyncio.TimeoutError:
        error = ProviderError("timeout", code="timeout")
    except Exception as e:
        error = ProviderError(str(e))
        log.exception("Ошибка генерации %s", gen_id)
    finally:
        if not ok:
            app.db.finish(gen_id, False)
        app.busy.discard(user.id)
        try:
            await status.delete()
        except Exception:
            pass
    if ok:
        await message.answer(f"−{final} кр, баланс {app.db.balance(user.id)} кр")
        return
    if error.code in ("insufficient_gems", "insufficient_credits"):
        await notify_admins(app, bot, f"⚠️ У провайдера {spec.provider} кончился баланс: {error}")
    await message.answer((error.user_message or "Не получилось сгенерировать.") + " Кредиты вернул")


async def send_media(message: Message, kind: str, media: Media) -> None:
    if media.data is not None:
        file = BufferedInputFile(media.data, filename=media.filename)
    elif media.url:
        file = URLInputFile(media.url, filename=media.filename)
    else:
        raise ProviderError("пустой результат")
    name = media.filename.lower()
    if name.endswith((".mp4", ".mov", ".webm")):
        await message.answer_video(file)
    elif kind == "image" and name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        try:
            await message.answer_photo(file)
        except Exception:
            # Большие картинки (2K+) телега может не принять как фото — шлём файлом.
            await message.answer_document(file)
    else:
        await message.answer_document(file)


# ---------- агент Vilva ----------

def agent_credits(app: App, vilva_units: float) -> int:
    return max(1, math.ceil(vilva_units * app.cfg.markup / app.cfg.vilva_credits_per_credit))


def agent_units(app: App, credits: int) -> float:
    """Бюджет в кредитах бота → бюджет в кредитах Vilva (без наценки)."""
    return round(credits * app.cfg.vilva_credits_per_credit / app.cfg.markup, 2)


async def cmd_agent(message: Message, command: CommandObject, app: App) -> None:
    await start_agent(message, app, (command.args or "").strip())


async def btn_agent(message: Message, app: App) -> None:
    await start_agent(message, app, "")


async def start_agent(message: Message, app: App, brief: str) -> None:
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    if not app.vilva:
        await message.answer("Агент сейчас недоступен")
        return
    if not brief:
        app.pending[message.from_user.id] = ("agent_brief",)
        await message.answer(
            "🤖 Опиши задачу для агента целиком. Например:\n"
            "«сделай 5 карточек для маркетплейса: беспроводные наушники, белый фон, инфографика с преимуществами, "
            "плюс 5-секундное видео для обложки»"
        )
        return
    await agent_choose_mode(message, app, brief)


async def agent_choose_mode(message: Message, app: App, brief: str) -> None:
    reason = check_prompt(brief)
    if reason:
        await message.answer(reason)
        return
    app.agent_drafts[message.from_user.id] = {"brief": brief}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Сначала план и смета", callback_data="am:plan_first")],
        [InlineKeyboardButton(text="🚀 Автопилот", callback_data="am:autopilot")],
    ])
    await message.answer(
        "Как работаем?\n📋 План — агент покажет план и смету, ты одобришь до траты кредитов\n"
        "🚀 Автопилот — сделает всё сам в рамках бюджета", reply_markup=kb)


async def cb_agent_mode(callback: CallbackQuery, app: App) -> None:
    mode = callback.data.split(":", 1)[1]
    draft = app.agent_drafts.get(callback.from_user.id)
    if not draft:
        await callback.answer("Задача потерялась, начни заново", show_alert=True)
        return
    draft["mode"] = mode
    rows = [[InlineKeyboardButton(text=f"{b} кр", callback_data=f"ab:{i}")] for i, b in enumerate(app.cfg.agent_budgets)]
    await safe_edit(callback, "Выбери максимальный бюджет. Неизрасходованное вернётся на баланс:",
                    InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def cb_agent_budget(callback: CallbackQuery, bot: Bot, app: App) -> None:
    uid = callback.from_user.id
    draft = app.agent_drafts.pop(uid, None)
    idx = callback.data.split(":", 1)[1]
    if not draft or "mode" not in draft or not idx.isdigit() or int(idx) >= len(app.cfg.agent_budgets):
        await callback.answer("Задача потерялась, начни заново", show_alert=True)
        return
    budget = app.cfg.agent_budgets[int(idx)]
    try:
        gen_id = app.db.charge(uid, "agent", draft["brief"], budget)
    except InsufficientCredits:
        await callback.message.answer(
            f"Не хватает кредитов: бюджет {budget}, у тебя {app.db.balance(uid)}.", reply_markup=packs_keyboard(app.cfg))
        await callback.answer()
        return
    await callback.answer()
    try:
        run_id = await app.vilva.agent_create(draft["brief"], draft["mode"], agent_units(app, budget))
    except ProviderError as e:
        app.db.finish(gen_id, False)
        log.warning("agent_create_run: %s", e)
        if e.code == "insufficient_credits":
            await notify_admins(app, bot, f"⚠️ У Vilva кончились кредиты: {e}")
        await callback.message.answer("Не удалось запустить агента, кредиты вернул. Попробуй позже")
        return
    app.db.add_agent_run(run_id, uid, gen_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⛔ Остановить", callback_data=f"ac:{gen_id}")]])
    await callback.message.answer(
        f"🤖 Агент запущен (бюджет до {budget} кр). Сообщу, когда будет план, вопрос или результат", reply_markup=kb)
    app.spawn(monitor_agent(app, bot, uid, run_id, gen_id, budget))


async def monitor_agent(app: App, bot: Bot, uid: int, run_id: str, gen_id: int, budget: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AGENT_TIMEOUT
    notified: set[str] = set()
    state = None
    while True:
        try:
            state = await app.vilva.agent_get(run_id)
        except ProviderError as e:
            log.warning("agent_get_run %s: %s", run_id, e)
            state = None
        if state is not None:
            if state.phase == "plan" and state.raw_status not in notified:
                notified.add(state.raw_status)
                await send_plan(app, bot, uid, run_id, gen_id)
            elif state.phase == "question" and state.raw_status + state.text not in notified:
                notified.add(state.raw_status + state.text)
                app.pending[uid] = ("agent_answer", gen_id)
                await bot.send_message(uid, f"🤖 Вопрос от агента:\n{state.text or 'нужно уточнение'}\n\nОтветь сообщением")
            elif state.phase in ("done", "failed", "cancelled"):
                break
        if loop.time() > deadline:
            try:
                await app.vilva.agent_cancel(run_id)
            except ProviderError:
                pass
            break
        await asyncio.sleep(app.agent_poll)

    used = state.credits_used if state else None
    charged = agent_credits(app, used) if used else None
    if state and state.phase == "done":
        final = app.db.finish(gen_id, True, min(charged, budget) if charged else None)
        await send_agent_result(bot, uid, state)
        await bot.send_message(uid, f"✅ Агент закончил. Списано {final} из {budget} кр, баланс {app.db.balance(uid)} кр")
    elif charged:
        final = app.db.finish(gen_id, True, min(charged, budget))
        await bot.send_message(uid, f"Агент остановлен. Списано за сделанное {final} кр, остальное вернул")
    else:
        app.db.finish(gen_id, False)
        await bot.send_message(uid, "Агент остановлен, кредиты вернул")


async def send_plan(app: App, bot: Bot, uid: int, run_id: str, gen_id: int) -> None:
    try:
        plan = await app.vilva.agent_plan(run_id)
    except ProviderError as e:
        log.warning("agent_get_plan %s: %s", run_id, e)
        plan = None
    text = plan if isinstance(plan, str) else _format_plan(plan)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Одобрить", callback_data=f"ap:{gen_id}:1"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"ap:{gen_id}:0"),
    ]])
    await bot.send_message(uid, f"📋 План агента:\n{text[:3500]}", reply_markup=kb)


def _format_plan(plan) -> str:
    import json

    if plan is None:
        return "агент ждёт одобрения"
    if isinstance(plan, dict):
        steps = plan.get("steps") or plan.get("plan") or plan.get("tasks")
        lines = []
        if isinstance(steps, list):
            for i, st in enumerate(steps, 1):
                title = st.get("title") or st.get("description") or st.get("name") if isinstance(st, dict) else st
                lines.append(f"{i}. {title}")
        est = plan.get("estimatedCredits") or plan.get("creditEstimate") or plan.get("estimate")
        if est is not None:
            lines.append(f"\nСмета Vilva: {est}")
        if lines:
            return "\n".join(lines)
    return json.dumps(plan, ensure_ascii=False, indent=1)


async def cb_agent_plan(callback: CallbackQuery, app: App) -> None:
    _, gen_id, approve = callback.data.split(":")
    run = app.db.agent_run_by_gen(int(gen_id)) if gen_id.isdigit() else None
    if run is None or run[1] != callback.from_user.id:
        await callback.answer("Запуск не найден", show_alert=True)
        return
    try:
        await app.vilva.agent_decide(run[0], approve == "1")
    except ProviderError as e:
        log.warning("agent decide: %s", e)
        await callback.answer("Не получилось, попробуй ещё раз", show_alert=True)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Одобрено, агент работает" if approve == "1" else "Отклонено")


async def cb_agent_cancel(callback: CallbackQuery, app: App) -> None:
    gen_id = callback.data.split(":", 1)[1]
    run = app.db.agent_run_by_gen(int(gen_id)) if gen_id.isdigit() else None
    if run is None or run[1] != callback.from_user.id:
        await callback.answer("Запуск не найден", show_alert=True)
        return
    try:
        await app.vilva.agent_cancel(run[0])
    except ProviderError as e:
        log.warning("agent cancel: %s", e)
    await callback.answer("Останавливаю…")


async def agent_send_answer(message: Message, app: App, gen_id: int) -> None:
    run = app.db.agent_run_by_gen(gen_id)
    if run is None:
        await message.answer("Запуск агента не найден")
        return
    try:
        await app.vilva.agent_answer(run[0], message.text)
        await message.answer("Передал агенту")
    except ProviderError as e:
        log.warning("agent_respond: %s", e)
        app.pending[message.from_user.id] = ("agent_answer", gen_id)
        await message.answer("Не получилось передать ответ, попробуй ещё раз")


async def send_agent_result(bot: Bot, uid: int, state) -> None:
    sent = 0
    for url in state.urls[:10]:
        path = url.lower().split("?", 1)[0]
        try:
            if path.endswith((".mp4", ".mov", ".webm")):
                await bot.send_video(uid, URLInputFile(url, filename="video.mp4"))
            elif path.endswith((".png", ".jpg", ".jpeg", ".webp")):
                await bot.send_photo(uid, URLInputFile(url, filename="image.png"))
            else:
                continue
            sent += 1
        except Exception:
            log.exception("Не удалось отправить результат агента %s", url)
            await bot.send_message(uid, url)
    if state.text:
        await bot.send_message(uid, state.text[:3500])
    if not sent and not state.text:
        await bot.send_message(uid, "Результат лежит в рабочем пространстве Vilva")


# ---------- роутер ----------

def create_router() -> Router:
    """Новый Router на каждый Dispatcher (Router нельзя подключить дважды)."""
    r = Router()
    r.message.register(cmd_start, CommandStart())
    r.message.register(cmd_help, Command("help"))
    r.message.register(cmd_balance, Command("balance"))
    r.message.register(cmd_balance, F.text == BTN_BALANCE)
    r.message.register(cmd_buy, Command("buy"))
    r.message.register(cmd_terms, Command("terms"))
    r.message.register(cmd_paysupport, Command("paysupport"))
    r.message.register(cmd_settings, Command("settings"))
    r.message.register(cmd_settings, F.text == BTN_SETTINGS)
    r.message.register(btn_kind, F.text.in_({BTN_IMAGE, BTN_VIDEO}))
    r.message.register(cmd_agent, Command("agent"))
    r.message.register(btn_agent, F.text == BTN_AGENT)
    r.message.register(cmd_stats, Command("stats"))
    r.message.register(cmd_refund, Command("refund"))
    r.message.register(on_payment, F.successful_payment)
    r.message.register(on_photo, F.photo)
    r.message.register(cmd_video, Command("video"))
    r.message.register(cmd_img, Command("img"))
    r.message.register(on_text, F.text & ~F.text.startswith("/"))

    r.callback_query.register(cb_settings, F.data == "settings")
    r.callback_query.register(cb_models, F.data.startswith("sm:"))
    r.callback_query.register(cb_pick_model, F.data.startswith("pm:"))
    r.callback_query.register(cb_toggle_advanced, F.data == "adv")
    r.callback_query.register(cb_params, F.data.startswith("sp:"))
    r.callback_query.register(cb_param_values, F.data.startswith("pp:"))
    r.callback_query.register(cb_set_value, F.data.startswith("pv:"))
    r.callback_query.register(cb_seed, F.data.startswith("ps:"))
    r.callback_query.register(cb_reset_params, F.data.startswith("pr:"))
    r.callback_query.register(cb_buy, F.data.startswith("buy:"))
    r.callback_query.register(cb_agent_mode, F.data.startswith("am:"))
    r.callback_query.register(cb_agent_budget, F.data.startswith("ab:"))
    r.callback_query.register(cb_agent_plan, F.data.startswith("ap:"))
    r.callback_query.register(cb_agent_cancel, F.data.startswith("ac:"))
    r.pre_checkout_query.register(pre_checkout)
    return r
