"""Настройки сайта. Всё секретное — из окружения: на сервере /etc/transcriber.env
(подставляет systemd), на своём компьютере — .env рядом с кодом."""
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # transcribe.py, summarize.py, make_docx.py лежат в корне

from envfile import load_env_file  # noqa: E402

load_env_file()


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Вход. Пока пользователь один; пароль хранится только хэшем argon2id.
LOGIN = os.getenv("TRANSCRIBER_LOGIN", "arkprhrv").strip()
PASSWORD_HASH = os.getenv("TRANSCRIBER_PASSWORD_HASH", "").strip()

# DEV=1 — локальный запуск по http://127.0.0.1: cookie без флага Secure.
DEV = _flag("TRANSCRIBER_DEV")
BASE_URL = os.getenv("TRANSCRIBER_BASE_URL", "https://transcriber.kahman.studio").rstrip("/")

DATA_DIR = Path(os.getenv("TRANSCRIBER_DATA_DIR", str(ROOT / "data"))).resolve()
CALLS_DIR = DATA_DIR / "calls"
DB_PATH = DATA_DIR / "transcriber.db"

TZ = ZoneInfo(os.getenv("TRANSCRIBER_TZ", "Asia/Yekaterinburg"))
# Имя того, кто ведёт разговор с этого телефона. Пусто — модель подпишет по смыслу
# («Исполнитель», «Менеджер»), не выдумывая имя.
OWNER_NAME = os.getenv("TRANSCRIBER_OWNER_NAME", "").strip()

MAX_UPLOAD_MB = int(os.getenv("TRANSCRIBER_MAX_UPLOAD_MB", "300"))
MAX_FILES_PER_UPLOAD = 10

SESSION_DAYS = 30
TRUSTED_DEVICE_DAYS = 90

# Telegram: запросы идут через свой Worker на Cloudflare (сервер в России).
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
TELEGRAM_PROXY_SECRET = os.getenv("TELEGRAM_PROXY_SECRET", "").strip()

COOKIE_SESSION = "tr_session" if DEV else "__Host-tr_session"
COOKIE_DEVICE = "tr_device" if DEV else "__Host-tr_device"
COOKIE_PENDING = "tr_pending" if DEV else "__Host-tr_pending"

CALLS_DIR.mkdir(parents=True, exist_ok=True)
