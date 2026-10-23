"""Сообщения владельцу в Telegram.

Сервер в России, поэтому запросы идут через свой Worker на Cloudflare
(cloudflare/telegram-worker.js): TELEGRAM_API_BASE — его адрес, а
TELEGRAM_PROXY_SECRET — пароль, без которого Worker никого не пускает.
Бот пишет только в один чат: TELEGRAM_CHAT_ID или чат, привязанный в настройках
сайта одноразовым кодом (start_link / finish_link). Чужой человек, написавший боту,
привязаться не может — он не знает кода.

Узнать свой chat id вручную (сначала напишите боту /start):
    python -m app.telegram chat-id
Проверить отправку:
    python -m app.telegram test
"""
import html
import json
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

import httpx

from . import config
from .db import del_setting, get_setting, set_setting

LINK_TTL = 15 * 60


def chat_id() -> str:
    return config.TELEGRAM_CHAT_ID or get_setting("telegram_chat_id")


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and chat_id())


def _call(method: str, data: dict | None = None, files: dict | None = None, timeout: float = 60) -> dict:
    url = f"{config.TELEGRAM_API_BASE}/bot{config.TELEGRAM_BOT_TOKEN}/{method}"
    headers = {"X-Proxy-Secret": config.TELEGRAM_PROXY_SECRET} if config.TELEGRAM_PROXY_SECRET else {}
    resp = httpx.post(url, data=data, files=files, headers=headers, timeout=timeout)
    try:
        body = resp.json()
    except ValueError:
        body = {"ok": False, "description": f"HTTP {resp.status_code}"}
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or f"HTTP {resp.status_code}")
    return body


def _reply(reply_to: int) -> dict:
    if not reply_to:
        return {}
    return {"reply_parameters": json.dumps({"message_id": reply_to, "allow_sending_without_reply": True})}


def send_message(text: str, reply_to: int = 0, chat: str = "") -> bool:
    """text — уже экранированный HTML (см. esc). chat — только для ответов боту
    из app/bot.py; уведомления всегда уходят владельцу."""
    chat = chat or chat_id()
    if not (config.TELEGRAM_BOT_TOKEN and chat):
        return False
    try:
        _call("sendMessage", {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML",
                              "disable_web_page_preview": "true", **_reply(reply_to)})
        return True
    except Exception as e:  # noqa: BLE001 — уведомление не должно ронять работу
        print(f"Telegram: не отправилось сообщение: {e}", file=sys.stderr)
        return False


def send_document(path: Path, filename: str, caption: str = "", reply_to: int = 0) -> bool:
    if not enabled():
        return False
    try:
        with path.open("rb") as f:
            _call("sendDocument", {"chat_id": chat_id(), "caption": caption[:1000],
                                   "parse_mode": "HTML", **_reply(reply_to)},
                  files={"document": (filename, f)}, timeout=180)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Telegram: не отправился файл: {e}", file=sys.stderr)
        return False


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def download(file_id: str, max_mb: int):
    """Файл, присланный боту, — во временный файл (удаляется при закрытии)."""
    file_path = _call("getFile", {"file_id": file_id}, timeout=30)["result"]["file_path"]
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", file_path) or ".." in file_path:
        raise RuntimeError("странный путь к файлу от Telegram")
    url = f"{config.TELEGRAM_API_BASE}/file/bot{config.TELEGRAM_BOT_TOKEN}/{file_path}"
    headers = {"X-Proxy-Secret": config.TELEGRAM_PROXY_SECRET} if config.TELEGRAM_PROXY_SECRET else {}
    out = tempfile.TemporaryFile()
    size, limit = 0, max_mb * 1024 * 1024
    with httpx.stream("GET", url, headers=headers, timeout=180) as resp:
        if resp.status_code != 200:
            out.close()
            raise RuntimeError(f"Telegram не отдал файл (HTTP {resp.status_code})")
        for chunk in resp.iter_bytes(1024 * 1024):
            size += len(chunk)
            if size > limit:
                out.close()
                raise RuntimeError(f"файл больше {max_mb} МБ")
            out.write(chunk)
    out.seek(0)
    return out


# ── привязка чата из настроек сайта ─────────────────────────────────────────

def start_link() -> tuple[str, str]:
    """Новый одноразовый код и ссылка t.me/<бот>?start=<код>: по ней Telegram сам
    отправит боту «/start <код>»."""
    username = _call("getMe", timeout=20)["result"]["username"]
    code = f"{secrets.randbelow(10 ** 8):08d}"
    set_setting("telegram_link_code", f"{code}:{int(time.time())}")
    set_setting("telegram_bot_username", username)
    return code, f"https://t.me/{username}?start={code}"


def pending_link() -> tuple[str, str] | None:
    raw = get_setting("telegram_link_code")
    if not raw:
        return None
    code, _, ts = raw.partition(":")
    if time.time() - int(ts or 0) > LINK_TTL:
        del_setting("telegram_link_code")
        return None
    return code, f"https://t.me/{get_setting('telegram_bot_username')}?start={code}"


def try_link(code: str, chat: int) -> bool:
    """Вызывает app/bot.py, когда боту пришло «/start <код>»."""
    link = pending_link()
    if not link or not secrets.compare_digest(link[0], code):
        return False
    set_setting("telegram_chat_id", str(chat))
    del_setting("telegram_link_code")
    return True


def finish_link() -> bool:
    """Кнопка «Проверить» в настройках. Когда работает приём записей (TELEGRAM_INBOX),
    сообщения читает app/bot.py и сам привязывает чат — здесь только смотрим результат.
    Иначе ищем среди новых сообщений боту личное сообщение с кодом."""
    if config.TELEGRAM_INBOX:
        return bool(chat_id())
    link = pending_link()
    if not link:
        return False
    code = link[0]
    updates = _call("getUpdates", {"timeout": "0", "allowed_updates": '["message"]'}, timeout=20)["result"]
    found = None
    for u in updates:
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        words = (msg.get("text") or "").split()
        if chat.get("type") == "private" and code in words[:2]:
            found = chat["id"]
    if updates:
        # Подтверждаем прочитанное, чтобы старые сообщения не мешали следующей привязке
        _call("getUpdates", {"timeout": "0", "offset": str(updates[-1]["update_id"] + 1)}, timeout=20)
    if found is None:
        return False
    set_setting("telegram_chat_id", str(found))
    del_setting("telegram_link_code")
    return True


def unlink():
    del_setting("telegram_chat_id")


def _cli():
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if not config.TELEGRAM_BOT_TOKEN:
        sys.exit("Не задан TELEGRAM_BOT_TOKEN")
    if cmd == "chat-id":
        updates = _call("getUpdates", {"timeout": "0"}).get("result", [])
        chats = {}
        for u in updates:
            msg = u.get("message") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                chats[chat["id"]] = chat.get("username") or chat.get("first_name") or ""
        if not chats:
            sys.exit("Пусто. Напишите боту /start и повторите.")
        for cid, name in chats.items():
            print(f"{cid}\t{name}")
    elif cmd == "test":
        ok = send_message("✅ Транскрибер на связи")
        sys.exit(0 if ok else "Не отправилось — см. ошибку выше")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    _cli()
