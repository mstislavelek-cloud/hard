"""Обработчики: меню, выбор моделей, продвинутые параметры, генерация, агент Vilva, оплата Stars."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import math
from dataclasses import dataclass, field

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    URLInputFile,
)

from .catalog import Catalog, ModelSpec, family_of
from .config import Config, Pack
from .db import Database, InsufficientCredits
from .moderation import check_prompt
from .providers import Media, ProviderError
from .providers.util import find_key
from .providers.vilva import extract_questions

log = logging.getLogger(__name__)

GENERATION_TIMEOUT = 960
AGENT_POLL = 6.0
AGENT_TIMEOUT = 45 * 60

BTN_IMAGE = "🖼 Картинка"
BTN_VIDEO = "🎬 Видео"
BTN_AGENT = "🤖 Агент"
BTN_SETTINGS = "⚙️ Настройки"
BTN_BALANCE = "💰 Баланс"
BTN_STOP_AGENT = "⛔ Остановить агента"
MENU_BUTTONS = {BTN_IMAGE, BTN_VIDEO, BTN_AGENT, BTN_SETTINGS, BTN_BALANCE}

PARAM_LABELS = {
    "aspect_ratio": "Формат",
    "resolution": "Разрешение",
    "duration": "Длительность, сек",
    "audio": "Звук",
    "quality": "Качество",
    "effort": "Effort",
    "prompt_extend": "Улучшить промпт",
    "prompt_enhance": "Улучшить промпт",
    "generate_audio": "Звук",
    "thinking_level": "Обдумывание",
    "web_search": "Поиск в интернете",
    "image_search": "Поиск картинок",
    "num_inference_steps": "Шаги",
    "guidance_scale": "Следование промпту",
    "prompt_weighting": "Веса в промпте",
    "hires": "Hi-res доработка",
    "adetailer_face": "Доработка лиц",
    "adetailer_hands": "Доработка рук",
    "negative_prompt": "Негатив",
}
VALUE_LABELS = {"true": "вкл", "false": "выкл", "minimal": "быстро", "high": "глубоко"}
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
    # Анкеты агента: gen_id → {"run_id", "uid", "questions", "answers"}
    agent_forms: dict[int, dict] = field(default_factory=dict)
    tasks: set[asyncio.Task] = field(default_factory=set)
    agent_poll: float = AGENT_POLL
    welcome_file_id: str | None = None

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


QUICK_PARAMS = ("aspect_ratio", "resolution", "duration")


def params_of(app: App, settings: dict, spec: ModelSpec) -> dict[str, str]:
    """Формат, разрешение и длительность выбираются всегда; остальное — только в продвинутом режиме."""
    params = {k: v for k, v in spec.simple.items() if k in spec.options}
    for k, v in settings["params"].get(spec.key, {}).items():
        if k in QUICK_PARAMS or settings.get("advanced"):
            if k == "seed" or k in spec.text_params or v in spec.options.get(k, ()):
                params[k] = v
    return params


def sorted_models(app: App, kind: str) -> list[ModelSpec]:
    def price(m):
        return app.catalog.price(m, {k: v for k, v in m.simple.items() if k in m.options})
    return sorted(app.catalog.by_kind(kind), key=lambda m: (price(m), m.provider, m.title))


def provider_tag(m: ModelSpec) -> str:
    """Только для админских отчётов: пользователю провайдеры не показываются."""
    return {"mage": "Mage", "vilva": "Vilva"}.get(m.provider, "тест")


PARAM_ICONS = {"aspect_ratio": "📐", "resolution": "🔍", "duration": "⏱"}
EXAMPLES = {
    "image": ("кроссовки на белом фоне, мягкий студийный свет",
              "уютная кофейня в дождливом Париже, акварель",
              "логотип лисы в минималистичном стиле"),
    "video": ("волны разбиваются о скалы на закате, медленный пролёт камеры",
              "кот в очках печатает на ноутбуке, крупный план",
              "неоновый город ночью, дрон пролетает между небоскрёбами"),
}


def kind_panel(app: App, user_id: int, kind: str) -> tuple[str, InlineKeyboardMarkup | None]:
    s = settings_of(app, user_id)
    spec = model_of(app, s, kind)
    if spec is None:
        return "Модели этого типа сейчас недоступны", None
    params = params_of(app, s, spec)
    rows = [[InlineKeyboardButton(text=f"🧠 Модель: {spec.title}", callback_data=f"sm:{kind}:k")]]
    quick = []
    for name in QUICK_PARAMS:
        if name in spec.options:
            v = params.get(name, spec.options[name][0])
            label = f"{v} сек" if name == "duration" else vlabel(v)
            quick.append(InlineKeyboardButton(text=f"{PARAM_ICONS[name]} {label}", callback_data=f"pp:{kind}:{name}:k"))
    if quick:
        rows.append(quick)
    rows.append([InlineKeyboardButton(text="🧪 Ещё настройки", callback_data=f"sx:{kind}")])
    example = EXAMPLES[kind][user_id % len(EXAMPLES[kind])]
    price = app.catalog.price(spec, params)
    photo_hint = "изменю его" if kind == "image" else "оживлю его"
    with_photo = app.catalog.price(spec, params, with_image=True)
    if spec.image_field and with_photo != price:
        photo_hint += f" (с фото — {with_photo} кр)"
    elif not spec.image_field:
        photo_hint = "эта модель работает только с текстом"
    title = "🖼 <b>Картинка</b>" if kind == "image" else "🎬 <b>Видео</b>"
    note = f"\n<i>{html.escape(spec.note)}</i>" if spec.note else ""
    text = (f"{title}\n\n"
            f"🧠 <b>{html.escape(spec.title)}</b>{note}\n"
            f"⚙️ {html.escape(describe_params(spec, params) or 'стандартные настройки')}\n"
            f"💎 Стоимость: <b>{price} кр</b> · на балансе {app.db.balance(user_id)} кр\n\n"
            f"✍️ Напиши, что {'нарисовать' if kind == 'image' else 'снять'}, например:\n"
            f"<i>«{example}»</i>\n"
            f"📎 {'Или пришли фото с подписью — ' if spec.image_field else ''}{photo_hint}")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def vlabel(v: str) -> str:
    """Подпись значения: true/false → вкл/выкл, 1k → 1K, 480P → 480p."""
    if v in VALUE_LABELS:
        return VALUE_LABELS[v]
    if re.fullmatch(r"\d+k", v):
        return v.upper()
    if re.fullmatch(r"\d+P", v):
        return v.lower()
    return v


def describe_params(spec: ModelSpec, params: dict[str, str]) -> str:
    parts = [f"{PARAM_LABELS.get(k, k)}: {vlabel(v)}" for k, v in params.items()
             if k != "seed" and k not in spec.text_params]
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


def agent_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_STOP_AGENT)]], resize_keyboard=True,
                               input_field_placeholder="Агент работает…")


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


def _range_text(lo: int, hi: int) -> str:
    return f"{lo} кр" if lo == hi else f"{lo}–{hi} кр"


def picker_families(app: App, kind: str) -> list[tuple[str, list[ModelSpec]]]:
    """Семейства моделей без разделения по провайдерам; одинаковые модели — самая дешёвая."""
    groups: dict[str, list[ModelSpec]] = {}
    seen: set[tuple[str, str]] = set()
    for m in sorted_models(app, kind):
        fam = family_of(m)
        title_key = (fam, re.sub(r"[^a-z0-9]", "", m.title.lower()))
        if title_key in seen:
            continue
        seen.add(title_key)
        groups.setdefault(fam, []).append(m)
    return sorted(groups.items(), key=lambda kv: (min(app.catalog.price_range(m)[0] for m in kv[1]), kv[0]))


def _back(kind: str, ret: str) -> str:
    return f"kp:{kind}" if ret == "k" else "settings"


def models_view(app: App, user_id: int, kind: str, ret: str = "s") -> tuple[str, InlineKeyboardMarkup]:
    """Шаг 1: семейство моделей."""
    current = model_of(app, settings_of(app, user_id), kind)
    rows, row = [], []
    for fi, (fam, models) in enumerate(picker_families(app, kind)):
        lo = min(app.catalog.price_range(m)[0] for m in models)
        mark = "✅ " if current and any(m.key == current.key for m in models) else ""
        row.append(InlineKeyboardButton(text=f"{mark}{fam} · от {lo}", callback_data=f"mm:{kind}:{fi}:{ret}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=_back(kind, ret))])
    text = (f"🧠 <b>Выбери семейство моделей</b> {'для картинок' if kind == 'image' else 'для видео'}\n"
            "Цена — от, в кредитах")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def family_models_view(app: App, user_id: int, kind: str, fi: int, ret: str):
    """Шаг 2: конкретная модель семейства."""
    families = picker_families(app, kind)
    fam, models = families[min(fi, len(families) - 1)]
    current = model_of(app, settings_of(app, user_id), kind)
    all_models = sorted_models(app, kind)
    lines = [f"🧠 <b>{html.escape(fam)}</b> — выбери модель\n"]
    rows = []
    for n, m in enumerate(models, 1):
        lo, hi = app.catalog.price_range(m)
        mark = "✅ " if current and m.key == current.key else ""
        note = f" — <i>{html.escape(m.note)}</i>" if m.note else ""
        lines.append(f"{n}. <b>{html.escape(m.title)}</b> · {_range_text(lo, hi)}{note}")
        rows.append([InlineKeyboardButton(text=f"{mark}{n}. {m.title} · {_range_text(lo, hi)}",
                                          callback_data=f"pm:{kind}:{all_models.index(m)}:{ret}")])
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"sm:{kind}:{ret}")])
    return "\n".join(lines)[:4000], InlineKeyboardMarkup(inline_keyboard=rows)


def params_view(app: App, user_id: int, kind: str) -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    spec = model_of(app, s, kind)
    params = params_of(app, s, spec)
    rows = []
    for name in spec.options:
        value = params.get(name, "по умолчанию")
        rows.append([InlineKeyboardButton(
            text=f"{PARAM_LABELS.get(name, name)}: {VALUE_LABELS.get(value, value)}", callback_data=f"pp:{kind}:{name}")])
    for name in spec.text_params:
        value = params.get(name)
        shown = (value[:20] + "…" if len(value) > 20 else value) if value else "нет"
        rows.append([InlineKeyboardButton(text=f"✏️ {PARAM_LABELS.get(name, name)}: {shown}",
                                          callback_data=f"pt:{kind}:{name}")])
    rows.append([InlineKeyboardButton(text=f"🎲 Seed: {params.get('seed', 'случайный')}", callback_data=f"ps:{kind}")])
    rows.append([
        InlineKeyboardButton(text="↩️ Сбросить", callback_data=f"pr:{kind}"),
        InlineKeyboardButton(text="« Назад", callback_data=f"kp:{kind}"),
    ])
    text = (f"🔧 {spec.title}\nТекущие: {describe_params(spec, params)}\n"
            f"Цена с этими параметрами: ~{app.catalog.price(spec, params)} кр")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def values_view(app: App, user_id: int, kind: str, name: str, ret: str = "p") -> tuple[str, InlineKeyboardMarkup]:
    s = settings_of(app, user_id)
    spec = model_of(app, s, kind)
    current = params_of(app, s, spec).get(name)
    rows, row = [], []
    for i, v in enumerate(spec.options.get(name, ())):
        price = app.catalog.price(spec, {**params_of(app, s, spec), name: v})
        label = f"{'✅ ' if v == current else ''}{VALUE_LABELS.get(v, v)} · ~{price}"
        row.append(InlineKeyboardButton(text=label, callback_data=f"pv:{kind}:{name}:{i}:{ret}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="« Назад", callback_data=f"kp:{kind}" if ret == "k" else f"sp:{kind}")])
    return f"{PARAM_LABELS.get(name, name)} для {spec.title} (цена в кредитах):", InlineKeyboardMarkup(inline_keyboard=rows)


WELCOME_IMAGE = os.path.join(os.path.dirname(__file__), "assets", "welcome.jpg")


def help_text(app: App) -> str:
    text = (
        f"<b>Как пользоваться {html.escape(app.cfg.brand)}</b>\n\n"
        f"{BTN_IMAGE} или {BTN_VIDEO} — выбери модель, формат и качество, потом просто пиши, что хочешь увидеть\n"
        "📎 <b>Фото с подписью</b> — изменю картинку или оживлю фото в видео\n"
        f"{BTN_SETTINGS} — модели и продвинутые параметры\n"
    )
    if app.vilva:
        text += f"{BTN_AGENT} — опиши задачу целиком: агент сам спланирует и сделает набор картинок и видео\n"
    return text + ("\n💎 Цена каждой генерации видна заранее, при ошибке кредиты возвращаются\n"
                   "/balance — баланс · /buy — пополнить · /terms — условия · /paysupport — помощь")


def welcome_text(app: App, name: str, is_new: bool) -> str:
    image_count = len(app.catalog.by_kind("image"))
    video_count = len(app.catalog.by_kind("video"))
    gift = f"\n🎁 Дарю <b>{app.cfg.free_credits} кредита</b>, чтобы попробовать\n" if is_new else ""
    agent = "\n🤖 <b>Агент</b> — опиши задачу целиком, он сам всё спланирует и сделает" if app.vilva else ""
    return (
        f"<b>Привет, {html.escape(name)}!</b> 👋\n\n"
        f"Это <b>{html.escape(app.cfg.brand)}</b> — студия нейросетей прямо в Telegram ✨\n\n"
        f"🖼 <b>Картинки</b> — {image_count} моделей: GPT Image, Nano Banana, Seedream, Flux, Midjourney и другие\n"
        f"🎬 <b>Видео</b> — {video_count} моделей: из текста или оживляю твоё фото"
        f"{agent}\n{gift}\n"
        "Жми кнопку внизу и пиши, что хочешь увидеть 👇"
    )


async def safe_edit(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Редактирует сообщение меню (HTML); если нельзя (например, это фото) — шлёт новое."""
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        if "not modified" in str(e):
            return
        await callback.message.answer(text, reply_markup=markup, parse_mode="HTML")


# ---------- базовые команды ----------

async def cmd_start(message: Message, app: App) -> None:
    user = message.from_user
    is_new = app.db.ensure_user(user.id, user.username, app.cfg.free_credits)
    text = welcome_text(app, user.first_name or "друг", is_new)
    if os.path.exists(WELCOME_IMAGE):
        photo = app.welcome_file_id or FSInputFile(WELCOME_IMAGE)
        try:
            sent = await message.answer_photo(photo, caption=text, parse_mode="HTML", reply_markup=main_menu(app))
            if sent.photo and not app.welcome_file_id:
                app.welcome_file_id = sent.photo[-1].file_id
            return
        except Exception:
            log.exception("Не удалось отправить картинку приветствия")
    await message.answer(text, parse_mode="HTML", reply_markup=main_menu(app))


async def cmd_help(message: Message, app: App) -> None:
    await message.answer(help_text(app), parse_mode="HTML", reply_markup=main_menu(app))


async def cmd_balance(message: Message, app: App) -> None:
    app.db.ensure_user(message.from_user.id, message.from_user.username, app.cfg.free_credits)
    bal = app.db.balance(message.from_user.id)
    s = settings_of(app, message.from_user.id)
    spec = model_of(app, s, "image")
    hint = ""
    if spec:
        per = app.catalog.price(spec, params_of(app, s, spec))
        hint = f"\nЭтого хватит примерно на <b>{bal // per}</b> картинок в {html.escape(spec.title)}" if per else ""
    await message.answer(f"💰 Баланс: <b>{bal} кр</b>{hint}\n\nПополнить за ⭐ Telegram Stars:",
                         parse_mode="HTML", reply_markup=packs_keyboard(app.cfg))


async def cmd_buy(message: Message, app: App) -> None:
    await message.answer("Выбери пакет:", reply_markup=packs_keyboard(app.cfg))


async def cmd_terms(message: Message, app: App) -> None:
    await message.answer(
        f"Условия {app.cfg.brand}:\n"
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
    text, kb = kind_panel(app, user.id, kind)
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- настройки: колбэки ----------

async def cb_settings(callback: CallbackQuery, app: App) -> None:
    text, kb = settings_view(app, callback.from_user.id)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_kind_panel(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    text, kb = kind_panel(app, callback.from_user.id, kind)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_advanced_from_panel(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    uid = callback.from_user.id
    s = settings_of(app, uid)
    if not s["advanced"]:
        s["advanced"] = True
        app.db.save_settings(uid, s)
    if model_of(app, s, kind) is None:
        await callback.answer("Нет моделей", show_alert=True)
        return
    text, kb = params_view(app, uid, kind)
    await safe_edit(callback, text, kb)
    await callback.answer("Продвинутый режим включён")


async def cb_models(callback: CallbackQuery, app: App) -> None:
    parts = callback.data.split(":")
    kind, ret = parts[1], (parts[2] if len(parts) > 2 else "s")
    text, kb = models_view(app, callback.from_user.id, kind, ret)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_family_models(callback: CallbackQuery, app: App) -> None:
    _, kind, fi, ret = callback.data.split(":")
    families = picker_families(app, kind)
    models = families[min(int(fi), len(families) - 1)][1]
    if len(models) == 1:
        # В семействе одна модель — выбираем сразу.
        callback_data = f"pm:{kind}:{sorted_models(app, kind).index(models[0])}:{ret}"
        await _pick(callback, app, callback_data)
        return
    text, kb = family_models_view(app, callback.from_user.id, kind, int(fi), ret)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_pick_model(callback: CallbackQuery, app: App) -> None:
    await _pick(callback, app, callback.data)


async def _pick(callback: CallbackQuery, app: App, data: str) -> None:
    parts = data.split(":")
    kind, idx, ret = parts[1], parts[2], (parts[3] if len(parts) > 3 else "s")
    models = sorted_models(app, kind)
    if not idx.isdigit() or int(idx) >= len(models):
        await callback.answer("Модель не найдена", show_alert=True)
        return
    uid = callback.from_user.id
    app.db.ensure_user(uid, callback.from_user.username, app.cfg.free_credits)
    s = settings_of(app, uid)
    s[f"{kind}_model"] = models[int(idx)].key
    app.db.save_settings(uid, s)
    text, kb = kind_panel(app, uid, kind) if ret == "k" else settings_view(app, uid)
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
    parts = callback.data.split(":")
    kind, name, ret = parts[1], parts[2], (parts[3] if len(parts) > 3 else "p")
    text, kb = values_view(app, callback.from_user.id, kind, name, ret)
    await safe_edit(callback, text, kb)
    await callback.answer()


async def cb_set_value(callback: CallbackQuery, app: App) -> None:
    parts = callback.data.split(":")
    kind, name, idx, ret = parts[1], parts[2], parts[3], (parts[4] if len(parts) > 4 else "p")
    uid = callback.from_user.id
    s = settings_of(app, uid)
    spec = model_of(app, s, kind)
    values = spec.options.get(name, ())
    if not idx.isdigit() or int(idx) >= len(values):
        await callback.answer("Значение не найдено", show_alert=True)
        return
    s["params"].setdefault(spec.key, {})[name] = values[int(idx)]
    app.db.save_settings(uid, s)
    text, kb = kind_panel(app, uid, kind) if ret == "k" else params_view(app, uid, kind)
    await safe_edit(callback, text, kb)
    await callback.answer("Сохранено")


async def cb_seed(callback: CallbackQuery, app: App) -> None:
    kind = callback.data.split(":")[1]
    app.pending[callback.from_user.id] = ("seed", kind)
    await callback.message.answer("Пришли seed числом (одинаковый seed даёт повторяемый результат) или 0 — случайный")
    await callback.answer()


async def cb_text_param(callback: CallbackQuery, app: App) -> None:
    _, kind, name = callback.data.split(":", 2)
    app.pending[callback.from_user.id] = ("text_param", kind, name)
    await callback.message.answer(f"Пришли текст для «{PARAM_LABELS.get(name, name)}» "
                                  "(например: blurry, lowres, extra fingers) или 0 — убрать")
    await callback.answer()


async def set_text_param(message: Message, app: App, kind: str, name: str) -> None:
    uid = message.from_user.id
    s = settings_of(app, uid)
    spec = model_of(app, s, kind)
    if name not in spec.text_params:
        await message.answer("У этой модели нет такого параметра")
        return
    text = (message.text or "").strip()
    p = s["params"].setdefault(spec.key, {})
    if text in ("", "0"):
        p.pop(name, None)
    else:
        p[name] = text[:500]
    app.db.save_settings(uid, s)
    t, kb = params_view(app, uid, kind)
    await message.answer(t, reply_markup=kb)


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


async def cmd_give(message: Message, command: CommandObject, bot: Bot, app: App) -> None:
    """/give <кредиты> [user_id] — начислить (минус — списать). Без user_id — себе."""
    if message.from_user.id not in app.cfg.admin_ids:
        return
    parts = (command.args or "").split()
    if not parts or not parts[0].lstrip("-").isdigit() or (len(parts) > 1 and not parts[1].isdigit()):
        await message.answer("Формат: /give 100 — себе, /give 100 123456789 — пользователю, /give -50 … — списать")
        return
    amount = int(parts[0])
    target = int(parts[1]) if len(parts) > 1 else message.from_user.id
    balance = app.db.grant(target, amount)
    log.info("Админ %s: %+d кр пользователю %s", message.from_user.id, amount, target)
    await message.answer(f"{'Начислил' if amount >= 0 else 'Списал'} {abs(amount)} кр пользователю {target}. Баланс: {balance} кр")
    if target != message.from_user.id and amount > 0:
        try:
            await bot.send_message(target, f"🎁 Тебе начислено {amount} кр. Баланс: {balance} кр")
        except Exception:
            pass


async def cmd_prices(message: Message, app: App) -> None:
    """Аудит цен: себестоимость и цена для пользователя по каждой модели при базовых и максимальных параметрах."""
    if message.from_user.id not in app.cfg.admin_ids:
        return
    lines = [f"Курсы: gem ${app.cfg.mage_usd_per_gem}, кредит Vilva ${app.cfg.vilva_usd_per_credit:.5f}, "
             f"кредит бота ${app.cfg.credit_usd}, наценка ×{app.cfg.markup}\n"]
    for kind in ("image", "video"):
        lines.append("🖼 Картинки" if kind == "image" else "\n🎬 Видео")
        for m in sorted_models(app, kind):
            base = {k: v for k, v in m.simple.items() if k in m.options}
            top = {k: v[-1] for k, v in m.options.items() if k in ("resolution", "duration", "quality", "effort")}
            unit = "gems" if m.provider == "mage" else "кр Vilva"
            for label, params in (("база", base), ("макс", {**base, **top})):
                u = app.catalog.estimate_units(m, params)
                usd = app.catalog.usd(m.provider, u)
                lines.append(f"{provider_tag(m)} · {m.title} [{label}: {describe_params(m, params)}] — {u:g} {unit} "
                             f"= ${usd:.3f} → {app.catalog.credits(m, u)} кр")
                if params == {**base, **top}:
                    break
    text = "\n".join(lines)
    for i in range(0, len(text), 3800):
        await message.answer(text[i:i + 3800])


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
    if state and state[0] == "text_param":
        await set_text_param(message, app, state[1], state[2])
        return
    if state and state[0] == "agent_brief":
        await agent_choose_mode(message, app, message.text)
        return
    if state and state[0] == "agent_answer":
        await agent_send_answer(message, app, state[1])
        return
    if state and state[0] == "agent_form_text":
        await agent_form_text(message, app, state[1], state[2])
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


async def generate(app: App, bot: Bot, message: Message, kind: str, prompt: str, image: bytes | None = None,
                   user=None) -> None:
    user = user or message.from_user
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
    cost = app.catalog.price(spec, params, with_image=image is not None)
    try:
        gen_id = app.db.charge(user.id, kind, prompt, cost)
    except InsufficientCredits:
        await message.answer(
            f"Не хватает кредитов: нужно ~{cost}, у тебя {app.db.balance(user.id)}.",
            reply_markup=packs_keyboard(app.cfg),
        )
        return

    app.busy.add(user.id)
    s["last_prompt"] = {**s.get("last_prompt", {}), kind: prompt}
    app.db.save_settings(user.id, s)
    wait = "обычно 1–3 минуты" if kind == "video" else "несколько секунд"
    verb = "🎬 Снимаю видео" if kind == "video" else "🎨 Рисую"
    status = await message.answer(f"{verb} в <b>{html.escape(spec.title)}</b>…\n⏳ {wait}", parse_mode="HTML")
    try:
        await bot.send_chat_action(message.chat.id, "upload_video" if kind == "video" else "upload_photo")
    except Exception:
        pass
    ok, final, error = False, cost, None
    try:
        result = await asyncio.wait_for(provider.generate(spec, prompt, params, image), GENERATION_TIMEOUT)
        actual = app.catalog.credits(spec, result.units) if result.units else None
        expected = actual if actual is not None else cost
        balance_after = app.db.balance(user.id) + cost - expected
        caption = (f"✨ <b>{html.escape(spec.title)}</b> · −{expected} кр · баланс {max(balance_after, 0)} кр")
        await send_media(message, kind, result.media, caption, result_keyboard(kind))
        ok = True
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
        return
    if error.code in ("insufficient_gems", "insufficient_credits"):
        await notify_admins(app, bot, f"⚠️ У провайдера {spec.provider} кончился баланс: {error}")
    await message.answer((error.user_message or "Не получилось сгенерировать.") + " Кредиты вернул")


def result_keyboard(kind: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Ещё вариант", callback_data=f"rg:{kind}"),
        InlineKeyboardButton(text="🎛 Параметры", callback_data=f"kp:{kind}"),
    ]])


async def cb_regenerate(callback: CallbackQuery, bot: Bot, app: App) -> None:
    kind = callback.data.split(":")[1]
    prompt = settings_of(app, callback.from_user.id).get("last_prompt", {}).get(kind)
    if not prompt:
        await callback.answer("Не нашёл прошлый запрос — напиши его заново", show_alert=True)
        return
    await callback.answer("Генерирую ещё вариант")
    await generate(app, bot, callback.message, kind, prompt, user=callback.from_user)


async def send_media(message: Message, kind: str, media: Media, caption: str | None = None,
                     markup: InlineKeyboardMarkup | None = None) -> None:
    if media.data is not None:
        file = BufferedInputFile(media.data, filename=media.filename)
    elif media.url:
        file = URLInputFile(media.url, filename=media.filename)
    else:
        raise ProviderError("пустой результат")
    name = media.filename.lower()
    extra = {"caption": caption, "parse_mode": "HTML", "reply_markup": markup} if caption else {}
    if name.endswith((".mp4", ".mov", ".webm")):
        await message.answer_video(file, **extra)
    elif kind == "image" and name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        try:
            await message.answer_photo(file, **extra)
        except Exception:
            # Большие картинки (2K+) телега может не принять как фото — шлём файлом.
            await message.answer_document(file, **extra)
    else:
        await message.answer_document(file, **extra)


# ---------- агент Vilva ----------

def agent_credits(app: App, vilva_units: float) -> int:
    """Кредиты Vilva → кредиты бота с наценкой."""
    usd = vilva_units * app.cfg.vilva_usd_per_credit
    return max(1, math.ceil(usd * app.cfg.markup / app.cfg.credit_usd - 1e-9))


def agent_units(app: App, credits: int) -> float:
    """Бюджет в кредитах бота → бюджет в кредитах Vilva (без наценки)."""
    return math.floor(credits * app.cfg.credit_usd / (app.cfg.markup * app.cfg.vilva_usd_per_credit))


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
    await callback.message.answer(
        f"🤖 Агент запущен (бюджет до {budget} кр). Сообщу, когда будет план, вопрос или результат.\n"
        f"Остановить можно кнопкой «{BTN_STOP_AGENT}» внизу", reply_markup=agent_keyboard())
    app.spawn(monitor_agent(app, bot, uid, run_id, gen_id, budget))


async def monitor_agent(app: App, bot: Bot, uid: int, run_id: str, gen_id: int, budget: int) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AGENT_TIMEOUT
    notified: set = set()
    state = None
    while True:
        try:
            state = await app.vilva.agent_get(run_id)
        except ProviderError as e:
            log.warning("agent_get_run %s: %s", run_id, e)
            state = None
        if state is not None and state.credits_used is not None and state.phase not in ("done", "failed", "cancelled"):
            cap_units = agent_units(app, app.db.generation_cost(gen_id))
            if state.credits_used > cap_units + max(2.0, cap_units * 0.1):
                log.error("agent %s превысил бюджет: %s > %s", run_id, state.credits_used, cap_units)
                try:
                    await app.vilva.agent_cancel(run_id)
                except ProviderError:
                    pass
                await notify_admins(app, bot, f"⚠️ Агент {run_id} превысил бюджет: потрачено {state.credits_used} "
                                              f"кредитов Vilva при лимите {cap_units}. Остановлен")
                await bot.send_message(uid, "⛔ Агент вышел за бюджет — остановил его")
                state.phase = "cancelled"
                break
        if state is not None:
            log_sig = (state.raw_status, state.resume_key, len(state.questions))
            if log_sig not in notified:
                notified.add(log_sig)
                log.info("agent %s: phase=%s status=%s payload=%s", run_id, state.phase, state.raw_status,
                         json.dumps(state.raw, ensure_ascii=False, default=str)[:3000])
            if state.phase in ("plan", "question"):
                sig = "pause:" + (state.resume_key or state.raw_status + "|".join(q.text for q in state.questions))
                if sig not in notified:
                    notified.add(sig)
                    await handle_pause(app, bot, uid, run_id, gen_id, state)
            elif state.phase in ("done", "failed", "cancelled"):
                break
        if loop.time() > deadline:
            try:
                await app.vilva.agent_cancel(run_id)
            except ProviderError:
                pass
            break
        await asyncio.sleep(app.agent_poll)

    app.agent_forms.pop(gen_id, None)
    if state and state.phase == "done":
        await bot.send_message(uid, "⏳ Агент закончил план, собираю результаты…")
        urls = await collect_results(app, run_id, state)
        # Генерации могли дописаться в рабочем пространстве уже после завершения запуска.
        try:
            state = await app.vilva.agent_get(run_id)
        except ProviderError:
            pass
        await send_agent_result(bot, uid, state, urls)
    cap = app.db.generation_cost(gen_id)
    used = state.credits_used if state else None
    charged = agent_credits(app, used) if used else None
    if state and state.phase == "done":
        final = app.db.finish(gen_id, True, min(charged, cap) if charged else None)
        await bot.send_message(uid, f"✅ Готово. Списано {final} из {cap} кр, баланс {app.db.balance(uid)} кр",
                               reply_markup=main_menu(app))
    elif charged:
        final = app.db.finish(gen_id, True, min(charged, cap))
        await bot.send_message(uid, f"Агент остановлен. Списано за сделанное {final} кр, остальное вернул",
                               reply_markup=main_menu(app))
    else:
        app.db.finish(gen_id, False)
        await bot.send_message(uid, "Агент остановлен, кредиты вернул", reply_markup=main_menu(app))


RESULTS_WAIT = 15 * 60
PENDING_ASSET = ("pending", "queued", "running", "processing", "generating", "in_progress", "in-progress",
                 "starting", "submitted")


async def collect_results(app: App, run_id: str, state) -> list[str]:
    """Ждёт, пока догенерируются ассеты в рабочем пространстве запуска, и возвращает их ссылки."""
    workspace = find_key(state.raw, "workspaceId", "workspace_id") if state else None
    if not workspace:
        return list(state.urls) if state else []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RESULTS_WAIT
    urls: list[str] = list(state.urls)
    logged = False
    stable = 0
    while True:
        try:
            data = await app.vilva.workspace_assets(str(workspace))
        except ProviderError as e:
            log.warning("list_workspace_assets %s: %s", workspace, e)
            return urls
        if not logged:
            logged = True
            log.info("agent %s assets payload=%s", run_id, json.dumps(data, ensure_ascii=False, default=str)[:3000])
        found, pending = _assets_status(data)
        for u in found:
            if u not in urls:
                urls.append(u)
        if not pending:
            stable += 1
            if urls or stable >= 3:
                return urls
        else:
            stable = 0
        if loop.time() > deadline:
            return urls
        await asyncio.sleep(max(app.agent_poll, 0.01) * 2)


def _assets_status(data) -> tuple[list[str], bool]:
    from .providers.vilva import IMAGE_EXT, VIDEO_EXT, _all_urls

    items: list = []
    if isinstance(data, dict):
        for k in ("assets", "items", "data", "results"):
            if isinstance(data.get(k), list):
                items = data[k]
                break
    elif isinstance(data, list):
        items = data
    pending = False
    for it in items:
        st = str(find_key(it, "status", "state") or "").lower() if isinstance(it, dict) else ""
        if st in PENDING_ASSET:
            pending = True
    media = [u for u in _all_urls(data) if u.lower().split("?", 1)[0].endswith(IMAGE_EXT + VIDEO_EXT)]
    return media, pending


async def handle_pause(app: App, bot: Bot, uid: int, run_id: str, gen_id: int, state) -> None:
    """Любая пауза Vilva: полный вопрос лежит в agent_get_plan."""
    plan = await _fetch_plan(app, run_id)
    question = plan.get("question") if isinstance(plan, dict) else None
    kind = str(question.get("kind") or question.get("type") or "") if isinstance(question, dict) else ""
    if "budget" in kind:
        await stop_on_budget(app, bot, uid, run_id, question)
        return
    questions = extract_questions(plan) or state.questions
    if questions:
        state.questions = questions
        intro = find_key(plan, "intro")
        await send_questions(app, bot, uid, run_id, gen_id, state, intro if isinstance(intro, str) else "")
        return
    if state.phase == "question":
        state.text = _question_text(plan) or state.text
        await send_questions(app, bot, uid, run_id, gen_id, state)
        return
    await send_plan(app, bot, uid, run_id, gen_id, plan)


async def stop_on_budget(app: App, bot: Bot, uid: int, run_id: str, question: dict) -> None:
    """Агент упёрся в лимит. Лимит не поднимаем: ответ «да» у Vilva снимает потолок без нашего контроля."""
    qid = next((q.get("id") for q in question.get("questions") or [] if isinstance(q, dict)), "budget_continue")
    try:
        await app.vilva.agent_answer(run_id, "Stop here", {qid: False})
    except ProviderError as e:
        log.warning("agent budget stop %s: %s", run_id, e)
        try:
            await app.vilva.agent_cancel(run_id)
        except ProviderError:
            pass
    await bot.send_message(
        uid, "💸 Агент упёрся в выбранный бюджет — останавливаю, чтобы не потратить больше. "
             "Пришлю то, что уже готово, неиспользованное верну. Для большой задачи запусти агента с бюджетом побольше")


async def send_budget_request(app: App, bot: Bot, uid: int, gen_id: int, question: dict) -> None:
    budget = question.get("budget") or {}
    estimate = question.get("estimate") or {}
    used, ceiling = float(budget.get("used") or 0), float(budget.get("ceiling") or 0)
    would_be = float(budget.get("wouldBe") or 0)
    need = float(estimate.get("totalCredits") or 0)
    extra_units = max(would_be - ceiling, need - (ceiling - used), 1.0)
    extra = agent_credits(app, extra_units)
    qid = next((q.get("id") for q in question.get("questions") or [] if isinstance(q, dict)), "budget_continue")
    app.agent_forms[gen_id] = {"budget": True, "uid": uid, "qid": qid, "extra": extra, "run_id": None}
    run = app.db.agent_run_by_gen(gen_id)
    if run:
        app.agent_forms[gen_id]["run_id"] = run[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✅ Добавить {extra} кр и продолжить", callback_data=f"ax:{gen_id}:1")],
        [InlineKeyboardButton(text="⛔ Остановиться здесь", callback_data=f"ax:{gen_id}:0")],
    ])
    await bot.send_message(
        uid,
        "💸 Агенту не хватает бюджета на следующий шаг.\n"
        f"Шагу нужно ещё ~{agent_credits(app, need) if need else extra} кр, а в лимите осталось "
        f"{agent_credits(app, max(ceiling - used, 0)) if ceiling > used else 0} кр.\n\n"
        f"Добавить {extra} кр к лимиту (спишется с баланса, неиспользованное вернётся) или остановиться? "
        "Если остановиться — уже сделанное останется, я пришлю результаты.\n"
        f"Баланс: {app.db.balance(uid)} кр",
        reply_markup=kb)


async def cb_agent_budget_raise(callback: CallbackQuery, bot: Bot, app: App) -> None:
    _, gen, yes = callback.data.split(":")
    form = app.agent_forms.get(int(gen)) if gen.isdigit() else None
    if not form or not form.get("budget") or form["uid"] != callback.from_user.id or not form.get("run_id"):
        await callback.answer("Вопрос уже неактуален", show_alert=True)
        return
    approve = yes == "1"
    if approve:
        try:
            app.db.extend(int(gen), form["extra"])
        except InsufficientCredits:
            await callback.answer(f"Не хватает кредитов: нужно {form['extra']}, у тебя "
                                  f"{app.db.balance(callback.from_user.id)}", show_alert=True)
            await callback.message.answer("Пополни баланс и нажми ещё раз:", reply_markup=packs_keyboard(app.cfg))
            return
    try:
        await app.vilva.agent_answer(form["run_id"], "Raise and fire" if approve else "Stop here",
                                     {form["qid"]: approve})
    except ProviderError as e:
        log.warning("agent budget respond: %s", e)
        if approve:
            app.db.extend(int(gen), -form["extra"])
        await callback.answer("Не получилось, попробуй ещё раз", show_alert=True)
        return
    app.agent_forms.pop(int(gen), None)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    await callback.message.answer(
        f"Лимит поднят на {form['extra']} кр, агент продолжает" if approve else "Ок, останавливаю агента и собираю результаты")


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _is_file(q) -> bool:
    return "file" in q.qtype or "upload" in q.qtype


def _question_keyboard(gen_id: int, qi: int, q, form: dict) -> InlineKeyboardMarkup | None:
    if _is_file(q):
        return None
    answer = form["answers"].get(qi)
    rows = []
    if q.multiple:
        chosen = answer if isinstance(answer, list) else []
        for oi, opt in enumerate(q.options[:15]):
            mark = "☑️ " if q.value_of(oi) in chosen else "⬜ "
            rows.append([InlineKeyboardButton(text=mark + _short(opt), callback_data=f"aq:{gen_id}:{qi}:{oi}")])
        custom = [c for c in chosen if c not in q.values]
        rows.append([InlineKeyboardButton(text=("✅ " if custom else "") + "✍️ Свой вариант",
                                          callback_data=f"aw:{gen_id}:{qi}")])
        done = qi in form["done"]
        rows.append([InlineKeyboardButton(text="✔️ Выбрано" if done else "✔️ Готово (выбери несколько)",
                                          callback_data=f"af:{gen_id}:{qi}")])
    elif q.options:
        for oi, opt in enumerate(q.options[:15]):
            mark = "✅ " if answer == q.value_of(oi) else ""
            rows.append([InlineKeyboardButton(text=mark + _short(opt), callback_data=f"aq:{gen_id}:{qi}:{oi}")])
        custom = answer is not None and answer not in q.values
        rows.append([InlineKeyboardButton(text=("✅ " if custom else "") + "✍️ Свой вариант",
                                          callback_data=f"aw:{gen_id}:{qi}")])
    else:
        rows.append([InlineKeyboardButton(text=("✅ " if answer else "") + "✍️ Ответить",
                                          callback_data=f"aw:{gen_id}:{qi}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _answered(form: dict, qi: int) -> bool:
    q = form["questions"][qi]
    a = form["answers"].get(qi)
    if q.multiple:
        return bool(a) and qi in form["done"]
    return a not in (None, "", [])


async def send_questions(app: App, bot: Bot, uid: int, run_id: str, gen_id: int, state, intro: str = "") -> None:
    questions = state.questions
    if not questions or (len(questions) == 1 and not questions[0].options and not questions[0].qtype):
        # Простой вопрос — ответ обычным сообщением.
        app.pending[uid] = ("agent_answer", gen_id)
        text = questions[0].text if questions else (state.text or "нужно уточнение")
        await bot.send_message(uid, f"🤖 Вопрос от агента:\n{text}\n\nОтветь сообщением")
        return
    form = {"run_id": run_id, "uid": uid, "questions": questions, "answers": {}, "done": set(), "msgs": {}}
    app.agent_forms[gen_id] = form
    head = f"🤖 {intro}\n\n" if intro else "🤖 "
    await bot.send_message(uid, head + f"Агенту нужны уточнения ({len(questions)}). Отвечай кнопками или текстом — "
                                       "отправлю, когда заполнишь обязательные (*)")
    for qi, q in enumerate(questions):
        text = f"{qi + 1}. {q.text}{' *' if q.required else ''}"
        if q.multiple:
            text += "\n(можно выбрать несколько)"
        if q.hint:
            text += f"\n💡 {q.hint}"
        if _is_file(q):
            text += "\n📎 Этот вопрос можно пропустить — агент обойдётся без фото"
        msg = await bot.send_message(uid, text, reply_markup=_question_keyboard(gen_id, qi, q, form))
        form["msgs"][qi] = getattr(msg, "message_id", None)
    await bot.send_message(uid, "Когда готово:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Отправить ответы", callback_data=f"as:{gen_id}")],
        [InlineKeyboardButton(text="🤷 Пусть агент решит сам", callback_data=f"ad:{gen_id}")],
    ]))


def _form_for(app: App, callback_data: str, uid: int) -> tuple[int, dict] | None:
    parts = callback_data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    form = app.agent_forms.get(int(parts[1]))
    if form is None or form["uid"] != uid:
        return None
    return int(parts[1]), form


def _fill_defaults(form: dict) -> None:
    """«Пусть агент решит сам»: незаполненные обязательные — первый вариант / «на твоё усмотрение»."""
    for qi, q in enumerate(form["questions"]):
        if _answered(form, qi) or _is_file(q) or not q.required:
            continue
        if q.multiple:
            form["answers"][qi] = form["answers"].get(qi) or [q.value_of(i) for i in range(min(3, len(q.options)))]
            form["done"].add(qi)
        elif q.options:
            form["answers"][qi] = q.value_of(0)
        else:
            form["answers"][qi] = "На твоё усмотрение"


async def submit_form(app: App, bot: Bot, gen_id: int, use_defaults: bool = False) -> bool:
    form = app.agent_forms.get(gen_id)
    if form is None:
        return False
    if use_defaults:
        _fill_defaults(form)
    qs = form["questions"]
    answers = {qs[i].id: (a == "true" if qs[i].qtype == "confirm" and a in ("true", "false") else a)
               for i, a in sorted(form["answers"].items()) if a not in (None, "", [])}
    lines = []
    for i, a in sorted(form["answers"].items()):
        q = qs[i]
        shown = a if isinstance(a, list) else [a]
        labels = [q.options[q.values.index(v)] if v in q.values else v for v in shown]
        lines.append(f"{i + 1}. {q.text} — {', '.join(labels)}")
    try:
        await app.vilva.agent_answer(form["run_id"], "\n".join(lines), answers)
    except ProviderError as e:
        log.warning("agent_respond: %s", e)
        await bot.send_message(form["uid"], "Не получилось передать ответы, попробуй ещё раз")
        return False
    app.agent_forms.pop(gen_id, None)
    await bot.send_message(form["uid"], "Передал агенту, работает дальше")
    return True


async def _maybe_autosubmit(app: App, bot: Bot, gen_id: int) -> None:
    form = app.agent_forms.get(gen_id)
    if form and all(_answered(form, i) for i, q in enumerate(form["questions"]) if not _is_file(q)):
        await submit_form(app, bot, gen_id)


async def _refresh_question(bot: Bot, form: dict, gen_id: int, qi: int, message=None) -> None:
    kb = _question_keyboard(gen_id, qi, form["questions"][qi], form)
    try:
        if message is not None:
            await message.edit_reply_markup(reply_markup=kb)
        elif form["msgs"].get(qi):
            await bot.edit_message_reply_markup(chat_id=form["uid"], message_id=form["msgs"][qi], reply_markup=kb)
    except Exception:
        pass


async def cb_agent_option(callback: CallbackQuery, bot: Bot, app: App) -> None:
    found = _form_for(app, callback.data, callback.from_user.id)
    if not found:
        await callback.answer("Вопрос уже неактуален", show_alert=True)
        return
    gen_id, form = found
    _, _, qi, oi = callback.data.split(":")
    qi, oi = int(qi), int(oi)
    q = form["questions"][qi]
    value = q.value_of(oi)
    if q.multiple:
        chosen = form["answers"].setdefault(qi, [])
        if value in chosen:
            chosen.remove(value)
        else:
            chosen.append(value)
        form["done"].discard(qi)
        await _refresh_question(bot, form, gen_id, qi, callback.message)
        await callback.answer(f"Выбрано: {len(chosen)}. Нажми «Готово», когда закончишь")
        return
    form["answers"][qi] = value
    await _refresh_question(bot, form, gen_id, qi, callback.message)
    await callback.answer("Принято")
    await _maybe_autosubmit(app, bot, gen_id)


async def cb_agent_done(callback: CallbackQuery, bot: Bot, app: App) -> None:
    found = _form_for(app, callback.data, callback.from_user.id)
    if not found:
        await callback.answer("Вопрос уже неактуален", show_alert=True)
        return
    gen_id, form = found
    qi = int(callback.data.split(":")[2])
    if not form["answers"].get(qi):
        await callback.answer("Выбери хотя бы один вариант", show_alert=True)
        return
    form["done"].add(qi)
    await _refresh_question(bot, form, gen_id, qi, callback.message)
    await callback.answer("Принято")
    await _maybe_autosubmit(app, bot, gen_id)


async def cb_agent_custom(callback: CallbackQuery, app: App) -> None:
    found = _form_for(app, callback.data, callback.from_user.id)
    if not found:
        await callback.answer("Вопрос уже неактуален", show_alert=True)
        return
    gen_id, form = found
    qi = int(callback.data.split(":")[2])
    q = form["questions"][qi]
    app.pending[callback.from_user.id] = ("agent_form_text", gen_id, qi)
    hint = f"\nНапример: {q.hint}" if q.hint and not q.options else ""
    await callback.message.answer(f"Напиши ответ: {q.text}{hint}")
    await callback.answer()


async def agent_form_text(message: Message, app: App, gen_id: int, qi: int) -> None:
    form = app.agent_forms.get(gen_id)
    if form is None:
        await message.answer("Вопрос уже неактуален")
        return
    q = form["questions"][qi]
    text = message.text.strip()
    if q.multiple:
        form["answers"].setdefault(qi, []).append(text)
        await message.answer("Добавил ✅ Отметь ещё варианты или нажми «Готово»")
    else:
        form["answers"][qi] = text
        await message.answer("Принято ✅")
    await _refresh_question(message.bot, form, gen_id, qi)
    await _maybe_autosubmit(app, message.bot, gen_id)


async def cb_agent_submit(callback: CallbackQuery, bot: Bot, app: App) -> None:
    found = _form_for(app, callback.data, callback.from_user.id)
    if not found:
        await callback.answer("Уже отправлено", show_alert=True)
        return
    gen_id, form = found
    for qi, q in enumerate(form["questions"]):
        if q.multiple and form["answers"].get(qi):
            form["done"].add(qi)
    missing = [i + 1 for i, q in enumerate(form["questions"])
               if q.required and not _is_file(q) and not _answered(form, i)]
    if missing:
        await callback.answer(f"Ответь на обязательные: {', '.join(map(str, missing))} — или «пусть решит сам»",
                              show_alert=True)
        return
    await callback.answer()
    await submit_form(app, bot, gen_id)


async def cb_agent_defaults(callback: CallbackQuery, bot: Bot, app: App) -> None:
    found = _form_for(app, callback.data, callback.from_user.id)
    if not found:
        await callback.answer("Уже отправлено", show_alert=True)
        return
    await callback.answer()
    await submit_form(app, bot, found[0], use_defaults=True)


async def _fetch_plan(app: App, run_id: str):
    try:
        plan = await app.vilva.agent_plan(run_id)
    except ProviderError as e:
        log.warning("agent_get_plan %s: %s", run_id, e)
        return None
    log.info("agent %s plan payload=%s", run_id, json.dumps(plan, ensure_ascii=False, default=str)[:4000])
    return plan


def _question_text(plan) -> str:
    if plan is None:
        return ""
    if isinstance(plan, str):
        return plan
    q = find_key(plan, "question", "prompt", "message", "text")
    return q if isinstance(q, str) and q.strip() else _format_plan(plan)


async def send_plan(app: App, bot: Bot, uid: int, run_id: str, gen_id: int, plan=None) -> None:
    if plan is None:
        plan = await _fetch_plan(app, run_id)
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
            lines.append(f"\nСмета: {est}")
        if lines:
            return "\n".join(lines)
        q = plan.get("question")
        intro = (q.get("intro") or q.get("summary") or q.get("title")) if isinstance(q, dict) else None
        if isinstance(intro, str) and intro.strip():
            return intro
        summary = find_key(plan, "summary", "intro", "description")
        if isinstance(summary, str) and summary.strip():
            return summary
    if isinstance(plan, str):
        return plan
    return "Агент ждёт подтверждения, чтобы продолжить"


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


async def btn_stop_agent(message: Message, app: App) -> None:
    runs = [r for r in app.db.pending_agent_runs() if r[1] == message.from_user.id]
    if not runs or not app.vilva:
        await message.answer("Активного агента нет", reply_markup=main_menu(app))
        return
    for run_id, _, _ in runs:
        try:
            await app.vilva.agent_cancel(run_id)
        except ProviderError as e:
            log.warning("agent cancel %s: %s", run_id, e)
    await message.answer("Останавливаю агента… пришлю, что успел сделать")


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


async def send_agent_result(bot: Bot, uid: int, state, urls: list[str] | None = None) -> None:
    sent = 0
    all_urls = list(dict.fromkeys((urls or []) + (list(state.urls) if state else [])))
    for url in all_urls[:20]:
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
    text = state.text if state else ""
    if text:
        await bot.send_message(uid, text[:3500])
    if not sent:
        await bot.send_message(uid, "Готовых картинок или видео агент не прислал — попробуй уточнить задачу")


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
    r.message.register(btn_stop_agent, F.text == BTN_STOP_AGENT)
    r.message.register(cmd_stats, Command("stats"))
    r.message.register(cmd_refund, Command("refund"))
    r.message.register(cmd_give, Command("give"))
    r.message.register(cmd_prices, Command("prices"))
    r.message.register(on_payment, F.successful_payment)
    r.message.register(on_photo, F.photo)
    r.message.register(cmd_video, Command("video"))
    r.message.register(cmd_img, Command("img"))
    r.message.register(on_text, F.text & ~F.text.startswith("/"))

    r.callback_query.register(cb_settings, F.data == "settings")
    r.callback_query.register(cb_models, F.data.startswith("sm:"))
    r.callback_query.register(cb_kind_panel, F.data.startswith("kp:"))
    r.callback_query.register(cb_regenerate, F.data.startswith("rg:"))
    r.callback_query.register(cb_family_models, F.data.startswith("mm:"))
    r.callback_query.register(cb_advanced_from_panel, F.data.startswith("sx:"))
    r.callback_query.register(cb_pick_model, F.data.startswith("pm:"))
    r.callback_query.register(cb_toggle_advanced, F.data == "adv")
    r.callback_query.register(cb_params, F.data.startswith("sp:"))
    r.callback_query.register(cb_param_values, F.data.startswith("pp:"))
    r.callback_query.register(cb_set_value, F.data.startswith("pv:"))
    r.callback_query.register(cb_seed, F.data.startswith("ps:"))
    r.callback_query.register(cb_text_param, F.data.startswith("pt:"))
    r.callback_query.register(cb_reset_params, F.data.startswith("pr:"))
    r.callback_query.register(cb_buy, F.data.startswith("buy:"))
    r.callback_query.register(cb_agent_mode, F.data.startswith("am:"))
    r.callback_query.register(cb_agent_budget, F.data.startswith("ab:"))
    r.callback_query.register(cb_agent_plan, F.data.startswith("ap:"))
    r.callback_query.register(cb_agent_cancel, F.data.startswith("ac:"))
    r.callback_query.register(cb_agent_option, F.data.startswith("aq:"))
    r.callback_query.register(cb_agent_custom, F.data.startswith("aw:"))
    r.callback_query.register(cb_agent_done, F.data.startswith("af:"))
    r.callback_query.register(cb_agent_budget_raise, F.data.startswith("ax:"))
    r.callback_query.register(cb_agent_submit, F.data.startswith("as:"))
    r.callback_query.register(cb_agent_defaults, F.data.startswith("ad:"))
    r.pre_checkout_query.register(pre_checkout)
    return r
