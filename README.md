# Gold Star Store

Telegram-бот и Telegram Mini App магазина **Gold Star** (Telegram Stars, Telegram Premium, Telegram Gifts) с серверным кошельком в **UZS** и ручным подтверждением пополнений администратором.

## Запуск на Render

- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn bot:app --workers 1 --threads 8`
- **Health Check Path:** `/healthz`
- **Persistent Disk:** подключите диск (например, `/var/data`) и укажите `DATABASE_PATH=/var/data/gold_star.db`, чтобы база SQLite сохранялась между деплоями и перезапусками.

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `BOT_TOKEN` | Да | Токен бота от `@BotFather` |
| `WEBHOOK_URL` | Да (для webhook) | Публичный HTTPS-адрес сервиса, например `https://gold-star-store.onrender.com` |
| `WEBHOOK_SECRET` | Да (для webhook) | Секретная строка для заголовка `X-Telegram-Bot-Api-Secret-Token` |
| `ADMIN_IDS` | Да | Telegram ID администраторов через запятую, например `123456789,987654321` |
| `TOPUP_REQUISITES` | Да | Реквизиты карты/счёта для ручного пополнения баланса |
| `WEBAPP_URL` | Нет | URL Mini App (по умолчанию совпадает с `WEBHOOK_URL`) |
| `DATABASE_PATH` | Нет | Путь к файлу SQLite (по умолчанию `/var/data/gold_star.db`) |
| `TOPUP_MIN_UZS` | Нет | Минимальная сумма заявки на пополнение в UZS (по умолчанию `5000`) |
| `TOPUP_MAX_UZS` | Нет | Максимальная сумма заявки на пополнение в UZS (по умолчанию `10000000`) |
| `FLASK_SECRET_KEY` | Нет | Секретный ключ сессий Flask |
| `INIT_DATA_MAX_AGE_SEC` | Нет | Максимальный возраст `auth_date` в Telegram `initData` в секундах (по умолчанию `86400`) |
| `WORKER_THREADS` | Нет | Число потоков обработки апдейтов (по умолчанию `8`) |
| `AUTO_SET_WEBHOOK` | Нет | Автоматически выставлять webhook при старте (`true` / `false`) |
| `PORT` | Нет | Порт при локальном запуске `python bot.py` (по умолчанию `8080`) |