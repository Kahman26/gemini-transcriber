# Telegram: бот и прокси на Cloudflare

Сервер стоит в России, поэтому в Telegram он ходит через свой Worker на Cloudflare —
так же, как в Gemini. Бот пишет только вам: готовые итоги с Word во вложении,
ошибки обработки, входы на сайт и попытки подобрать пароль.

## 1. Бот

1. В Telegram откройте @BotFather → `/newbot`.
2. Имя — любое, например «Транскрибер». Адрес — любой свободный, заканчивается на `bot`.
3. BotFather пришлёт токен вида `1234567890:AA...`. Это пароль от бота — никому не показывайте.
4. Откройте своего бота и нажмите «Старт» (`/start`), иначе он не сможет вам писать.

## 2. Worker на Cloudflare

1. dash.cloudflare.com → Workers & Pages → Create → Create Worker.
2. Имя: `telegram-proxy` → Deploy.
3. Edit code → удалите всё и вставьте содержимое `cloudflare/telegram-worker.js` → Deploy.
4. Settings → Variables and Secrets → Add:
   - Type: **Secret**, Name: `PROXY_SECRET`, Value: длинная случайная строка
     (например, из генератора паролей, 40+ символов) → Deploy.
5. Адрес Worker'а виден вверху: `https://telegram-proxy.<ваш-поддомен>.workers.dev`.

## 3. Настройки на сервере

В `/etc/transcriber.env`:

```
TELEGRAM_BOT_TOKEN=токен от BotFather
TELEGRAM_API_BASE=https://telegram-proxy.<ваш-поддомен>.workers.dev
TELEGRAM_PROXY_SECRET=та же строка, что в PROXY_SECRET
TELEGRAM_CHAT_ID=
```

Узнать `TELEGRAM_CHAT_ID` (после `/start` боту):

```
cd /opt/transcriber && PYTHONDONTWRITEBYTECODE=1 venv/bin/python -m app.telegram chat-id
```

Вписать число в `TELEGRAM_CHAT_ID`, затем:

```
systemctl restart transcriber transcriber-worker
```

Проверка — «Настройки» на сайте → «Отправить проверку».
