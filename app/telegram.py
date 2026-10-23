"""Сообщения владельцу в Telegram.

Сервер в России, поэтому запросы идут через свой Worker на Cloudflare
(cloudflare/telegram-worker.js): TELEGRAM_API_BASE — его адрес, а
TELEGRAM_PROXY_SECRET — пароль, без которого Worker никого не пускает.
Бот пишет только в TELEGRAM_CHAT_ID.

Узнать свой chat id (сначала напишите боту /start):
    python -m app.telegram chat-id
Проверить отправку:
    python -m app.telegram test
"""
import html
import sys
from pathlib import Path

import httpx

from . import config


def enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


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


def send_message(text: str) -> bool:
    """text — уже экранированный HTML (см. esc)."""
    if not enabled():
        return False
    try:
        _call("sendMessage", {"chat_id": config.TELEGRAM_CHAT_ID, "text": text[:4000],
                              "parse_mode": "HTML", "disable_web_page_preview": "true"})
        return True
    except Exception as e:  # noqa: BLE001 — уведомление не должно ронять работу
        print(f"Telegram: не отправилось сообщение: {e}", file=sys.stderr)
        return False


def send_document(path: Path, filename: str, caption: str = "") -> bool:
    if not enabled():
        return False
    try:
        with path.open("rb") as f:
            _call("sendDocument", {"chat_id": config.TELEGRAM_CHAT_ID, "caption": caption[:1000],
                                   "parse_mode": "HTML"},
                  files={"document": (filename, f)}, timeout=180)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Telegram: не отправился файл: {e}", file=sys.stderr)
        return False


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


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
