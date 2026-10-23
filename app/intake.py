"""Приём записей: с сайта (кнопка «Загрузить») и от бота в Telegram.

Файл сохраняется под сгенерированным именем, ffmpeg проверяет, что это звук,
звонок встаёт в очередь — дальше его забирает app/worker.py.
"""
import re
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO

from summarize import TEMPLATES

from . import config
from .db import db, now
from .media import probe, safe_ext


class UploadError(Exception):
    pass


def clean_contact(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:80]


def clean_template(value) -> str:
    value = str(value or "auto")
    return value if value in TEMPLATES else "auto"


def store(stream: BinaryIO, filename: str, contact: str = "", template: str = "auto",
          origin: str = "web", tg_message_id: int = 0, max_mb: int | None = None) -> str:
    """Возвращает id звонка. При ошибке ничего не оставляет на диске и бросает UploadError."""
    call_id = uuid.uuid4().hex
    d = config.CALLS_DIR / call_id
    d.mkdir(parents=True)
    ext = safe_ext(filename)
    src = d / f"source.{ext}"
    max_mb = max_mb or config.MAX_UPLOAD_MB
    limit = max_mb * 1024 * 1024
    size = 0
    try:
        with src.open("wb") as out:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UploadError(f"Файл больше {max_mb} МБ")
                out.write(chunk)
        if size == 0:
            raise UploadError("Пустой файл")
        has_audio, duration = probe(src)
        if not has_audio:
            raise UploadError("Это не похоже на аудиозапись")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise

    original = re.sub(r"[\x00-\x1f\x7f]", "", Path(filename or "").name)[:200] or f"запись.{ext}"
    t = now()
    with db() as con:
        con.execute(
            "INSERT INTO calls(id, created_at, updated_at, contact, template, original_name, source_ext, origin, "
            "size_bytes, duration_sec, status, redo, message, tg_message_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'all', 'В очереди', ?)",
            (call_id, t, t, clean_contact(contact), clean_template(template), original, ext, origin,
             size, duration, tg_message_id))
    return call_id
