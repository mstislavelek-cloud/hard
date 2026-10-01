# genbot — telegram-бот генерации картинок и видео за Stars

Пишешь текст и получаешь картинку. `/video текст` даёт видео на 5 секунд, а фото с подписью `/video текст` оживляет это фото. Оплата идёт пакетами кредитов в Telegram Stars (валюта XTR, отдельный платёжный провайдер не нужен).

## что умеет
- 3 бесплатных кредита новичку, картинка стоит 1 кредит, видео 15 (меняется в `.env`)
- пакеты за 100, 250 и 600 ⭐; повторное зачисление одного платежа исключено
- если генерация упала или превысила таймаут, кредиты возвращаются автоматически
- фильтр 18+ и несовершеннолетних, у fal дополнительно включён safety checker
- одна генерация на пользователя одновременно
- `/terms` и `/paysupport` — их требует telegram для ботов со Stars
- админам доступны `/stats` и `/refund <charge_id>` (возврат Stars)
- провайдеры: `mage` (api.mage.space, картинки и видео), `vilva` (их mcp-сервер, картинки и видео), `fal`, `openrouter` (только картинки) и `mock` (тест без затрат)
- `python -m bot.check` проверяет ключ vilva: выводит инструменты, баланс и модели, кредиты не тратит

## запуск локально (windows)
```powershell
cd genbot
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # вписать BOT_TOKEN, остальное можно оставить mock
py -m bot.main           # .env подхватывается автоматически
```

## запуск на vps (ubuntu)
```bash
sudo mkdir -p /opt/genbot && sudo cp -r . /opt/genbot && cd /opt/genbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
sudo cp deploy/genbot.service /etc/systemd/system/
sudo systemctl enable --now genbot
journalctl -u genbot -f
```

## тесты
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## экономика (проверить цены перед запуском)
- разработчик получает примерно $13.3 за 1000 ⭐, вывод через fragment после 21 дня холда
- пакет за 100 ⭐ даёт ~$1.33 на 20 кредитов, это ~$0.066 за кредит
- картинка flux schnell на fal стоит доли цента, gemini image через openrouter — несколько центов
- видео kling 5 сек на fal стоит порядка десятков центов, поэтому оно и стоит 15 кредитов (~$1)

Актуальные цены моделей смотри на fal.ai/models и openrouter.ai перед тем, как менять `IMAGE_COST` и `VIDEO_COST`.
