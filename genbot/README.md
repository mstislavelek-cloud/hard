# genbot — telegram-бот генерации картинок и видео за Stars (Mage + Vilva)

## что умеет
- **меню:** 🖼 Картинка / 🎬 Видео / 🤖 Агент / ⚙️ Настройки / 💰 Баланс
- **генерация:** пишешь текст и получаешь результат. фото с подписью в режиме картинок даёт правку, в режиме видео оживляет фото
- **выбор модели** в ⚙️ Настройки, цена в кредитах видна сразу
  - mage, картинки: gpt image 2.5 flare, guava 2 / 2 pro, mango 3 / 3s / 2 (до 4K)
  - mage, видео: lemon, cherry mini / cherry / cherry 2 pro
  - vilva: модели подтягиваются при старте из `list_models` вместе с ценами
- **продвинутый режим:** формат, разрешение, длительность, звук, seed. цена пересчитывается на каждой кнопке
- **агент vilva:** задача, потом режим (📋 сначала план и смета с кнопками одобрить/отклонить или 🚀 автопилот), потом бюджет. агент может задать вопрос, ответ уходит ему. можно ⛔ остановить. списывается фактический расход, остаток бюджета возвращается
- **оплата:** пакеты кредитов в stars (XTR). цена резервируется заранее и пересчитывается по фактическому списанию у провайдера (mage: `billing.gems_charged`). при ошибке кредиты возвращаются
- **безопасность:** фильтр 18+ и несовершеннолетних, плюс модерация mage. админам приходит уведомление, если у провайдера кончился баланс
- **для админа:** `/stats`, `/refund <charge_id>`, `/give <кр> [user_id]` (выдать кредиты, минус — списать). есть `/terms` и `/paysupport`
- незавершённые запуски агента доотслеживаются после перезапуска бота

## запуск (windows)
```powershell
cd genbot
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # вписать BOT_TOKEN, ADMIN_IDS и ключи
py -m bot.check          # проверка ключей mage и vilva, ничего не тратит
py -m bot.main
```

## запуск на vps (ubuntu)
```bash
sudo mkdir -p /opt/genbot && sudo cp -r . /opt/genbot && cd /opt/genbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/python -m bot.check
sudo cp deploy/genbot.service /etc/systemd/system/ && sudo systemctl enable --now genbot
journalctl -u genbot -f
```

## экономика
- 1 кредит бота ≈ $0.066: пакет 100 ⭐ даёт ~$1.33 на 20 кредитов
- `GEMS_PER_CREDIT` и `VILVA_CREDITS_PER_CREDIT` задают, сколько единиц провайдера стоит один кредит до наценки. считается как 0.066 / цена единицы в $. цену gem смотри в mage → api → billing
- `MARKUP` — наценка, по умолчанию ×2
- цены моделей mage в gems взяты из их каталога (2026-10); фактическое списание всё равно приходит в ответе

## тесты
```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
