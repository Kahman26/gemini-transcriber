"""Приём записей через бота в Telegram.

Запись из Заметок → «Поделиться» → Telegram → чат с ботом. В подписи можно
указать собеседника и тип разговора — и то и другое необязательно:
    Иван              — только имя
    Иван холодный     — холодный звонок
    Иван заказчик     — созвон с заказчиком
Итог и Word бот присылает ответом на сообщение с записью (см. app/worker.py).

Работает потоком внутри службы очереди, если TELEGRAM_INBOX=1. Файлы
принимаются только из привязанного чата владельца; чужим бот не отвечает
ничем, кроме короткого отказа на /start.
"""
import sys
import time
import traceback

from summarize import KINDS

from . import config, intake, telegram
from .db import get_setting, set_setting

COLD_WORDS = {"холодный", "холодная", "холодное", "холодн", "cold"}
CLIENT_WORDS = {"заказчик", "заказчиком", "заказчика", "клиент", "клиентом", "созвон", "client"}
AUTO_WORDS = {"авто", "auto"}
FILLER_WORDS = {"с", "со", "звонок", "звонка"}

HELP = (
    "Пришлите сюда запись звонка — файлом или через «Поделиться» → Telegram.\n\n"
    "В подписи можно указать собеседника и тип разговора (необязательно):\n"
    "• <code>Иван</code> — только имя\n"
    "• <code>Иван холодный</code> — холодный звонок\n"
    "• <code>Иван заказчик</code> — созвон с заказчиком\n"
    "Без типа он определится сам.\n\n"
    "Итог и Word придут ответом на сообщение с записью. "
    "Файлы больше {mb} МБ Telegram боту не отдаёт — их загрузите на сайте: {url}/upload"
)


def parse_caption(text: str) -> tuple[str, str]:
    """«Иван, холодный» → («Иван», "cold"). Слова типа убираются из имени."""
    template, rest = "auto", []
    for word in (text or "").split():
        key = word.lower().strip("#.,;:!?()«»\"'—-")
        if key in COLD_WORDS:
            template = "cold"
        elif key in CLIENT_WORDS:
            template = "client"
        elif key in AUTO_WORDS:
            template = "auto"
        elif key and key not in FILLER_WORDS:
            rest.append(word)
    return " ".join(rest).strip(" ,.;:—-"), template


def extract_file(msg: dict) -> dict | None:
    for kind, default_name in (("audio", "audio.m4a"), ("voice", "voice.ogg"), ("document", "file"),
                               ("video", "video.mp4"), ("video_note", "video_note.mp4")):
        f = msg.get(kind)
        if f and f.get("file_id"):
            return {"id": f["file_id"], "size": int(f.get("file_size") or 0),
                    "name": f.get("file_name") or default_name}
    return None


def help_text() -> str:
    return HELP.format(mb=config.TELEGRAM_MAX_FILE_MB, url=telegram.esc(config.BASE_URL))


def handle(msg: dict):
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id, msg_id = chat["id"], msg["message_id"]
    text = (msg.get("text") or "").strip()
    owner = str(chat_id) == telegram.chat_id()

    if text.startswith("/start"):
        parts = text.split()
        if len(parts) > 1 and telegram.try_link(parts[1], chat_id):
            telegram.send_message("✅ Чат привязан к транскриберу.\n\n" + help_text(), chat=str(chat_id))
        elif owner:
            telegram.send_message(help_text(), chat=str(chat_id))
        else:
            telegram.send_message("Это личный бот, он отвечает только владельцу.", chat=str(chat_id))
        return
    if not owner:
        print(f"BOT_FOREIGN chat={chat_id}", file=sys.stderr, flush=True)
        return

    file = extract_file(msg)
    if not file:
        telegram.send_message(help_text(), reply_to=msg_id)
        return
    if file["size"] > config.TELEGRAM_MAX_FILE_MB * 1024 * 1024:
        telegram.send_message(
            f"❌ Файл больше {config.TELEGRAM_MAX_FILE_MB} МБ — Telegram не отдаёт такие ботам. "
            f"Загрузите его на сайте: {telegram.esc(config.BASE_URL)}/upload", reply_to=msg_id)
        return

    contact, template = parse_caption(msg.get("caption", ""))
    try:
        with telegram.download(file["id"], config.TELEGRAM_MAX_FILE_MB) as stream:
            call_id = intake.store(stream, file["name"], contact, template, origin="telegram",
                                   tg_message_id=msg_id, max_mb=config.TELEGRAM_MAX_FILE_MB)
    except intake.UploadError as e:
        telegram.send_message(f"❌ Не принято: {telegram.esc(str(e))}", reply_to=msg_id)
        return
    except Exception as e:  # noqa: BLE001 — сбой сети или прокси: сказать владельцу, не падать
        print(f"Бот: не скачался файл: {e}", file=sys.stderr, flush=True)
        telegram.send_message("❌ Не получилось скачать файл из Telegram. Попробуйте ещё раз "
                              f"или загрузите на сайте: {telegram.esc(config.BASE_URL)}/upload", reply_to=msg_id)
        return

    who = f" <b>{telegram.esc(contact)}</b>" if contact else ""
    kind = KINDS.get(template, "тип определю сам").lower() if template != "auto" else "тип определю сам"
    telegram.send_message(f"✅ Принято{who} · {kind}. Итог пришлю ответом, обычно это 1–3 минуты.\n"
                          f"{telegram.esc(config.BASE_URL)}/calls/{call_id}", reply_to=msg_id)


def run_inbox():
    """Длинный опрос getUpdates через Worker. Номер последнего обработанного
    сообщения хранится в базе — после перезапуска ничего не теряется и не дублируется."""
    offset = int(get_setting("telegram_offset", "0") or 0)
    print("Бот: приём записей включён", flush=True)
    while True:
        try:
            updates = telegram._call("getUpdates", {"timeout": "25", "offset": str(offset),
                                                    "allowed_updates": '["message"]'}, timeout=40)["result"]
        except Exception as e:  # noqa: BLE001
            print(f"Бот: getUpdates не ответил: {e}", file=sys.stderr, flush=True)
            time.sleep(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            # Сначала запоминаем, потом обрабатываем: сообщение, на котором всё падает,
            # не будет повторяться по кругу
            set_setting("telegram_offset", str(offset))
            if "message" in update:
                try:
                    handle(update["message"])
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
