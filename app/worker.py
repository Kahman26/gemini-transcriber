"""Очередь обработки звонков. Отдельный процесс (служба transcriber-worker):

    python -m app.worker

Берёт звонки со статусом queued по одному: копия для прослушивания (m4a) →
расшифровка → итог → Word → сообщение в Telegram. Куски расшифровки
сохраняются, поэтому после перезапуска работа продолжается с места обрыва.
"""
import shutil
import sys
import time
import traceback

import make_docx
import summarize
import transcribe

from . import config, telegram
from .db import db, init, now
from .media import to_m4a
from .render import download_name, fmt_date_full, fmt_duration

POLL_SEC = 2


def log(msg: str):
    print(msg, flush=True)


def call_dir(call_id: str):
    return config.CALLS_DIR / call_id


def update(call_id: str, **fields):
    fields["updated_at"] = now()
    cols = ", ".join(f"{k} = ?" for k in fields)  # имена колонок — только наши, из кода
    with db() as con:
        con.execute(f"UPDATE calls SET {cols} WHERE id = ?", (*fields.values(), call_id))


def take_next():
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM calls WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
        if row:
            con.execute("UPDATE calls SET status = 'processing', progress = 0, message = 'Начинаю', "
                        "error = '', updated_at = ? WHERE id = ?", (now(), row["id"]))
        con.execute("COMMIT")
    return row


def context_for(call) -> str:
    """Подсказка модели — только участники (иначе она вставляет лишнее в текст)."""
    parts = []
    if config.OWNER_NAME:
        parts.append(f"{config.OWNER_NAME} — владелец телефона, ведёт разговор")
    if call["contact"]:
        parts.append(f"{call['contact']} — собеседник")
    return "; ".join(parts)


def summary_header(call, kind: str, duration: int) -> str:
    who = call["contact"] or "собеседник не указан"
    return (f"# Итог разговора: {who}\n"
            f"_{fmt_date_full(call['created_at'])} · {fmt_duration(duration)} · "
            f"{summarize.KINDS.get(kind, 'Разговор')}_\n\n")


def process(call):
    cid = call["id"]
    d = call_dir(cid)
    src = d / f"source.{call['source_ext']}"
    audio = d / "audio.m4a"
    transcript_path = d / "transcript.txt"
    context = context_for(call)

    if not audio.exists():
        update(cid, progress=2, message="Готовлю запись")
        to_m4a(src, audio)

    if call["redo"] == "all" or not transcript_path.exists():
        def progress(done, total, msg):
            update(cid, progress=5 + int(80 * done / max(total, 1)), message=msg)
        text, duration, models = transcribe.transcribe_files(
            [src], d / ".chunks", context, progress=progress, log=log)
        transcript_path.write_text(text, encoding="utf-8")
        shutil.rmtree(d / ".chunks", ignore_errors=True)
        update(cid, duration_sec=duration or call["duration_sec"], models=", ".join(models))
    else:
        text = transcript_path.read_text(encoding="utf-8")

    update(cid, progress=88, message="Пишу итог")
    md, kind, model = summarize.summarize(text, call["template"], context, log)
    (d / "summary.md").write_text(md, encoding="utf-8")

    with db() as con:
        fresh = con.execute("SELECT * FROM calls WHERE id = ?", (cid,)).fetchone()
    update(cid, progress=96, message="Собираю Word")
    make_docx.build_docx(summary_header(fresh, kind, fresh["duration_sec"]) + md, d / "summary.docx",
                         transcript=text)

    models_used = ", ".join(dict.fromkeys([m for m in fresh["models"].split(", ") if m] + [model]))
    update(cid, status="done", kind=kind, progress=100, message="Готово", redo="summary",
           short_summary=summarize.short_summary(md), models=models_used)
    notify_done(cid)


def notify_done(cid: str):
    if not telegram.enabled():
        return
    with db() as con:
        call = con.execute("SELECT * FROM calls WHERE id = ?", (cid,)).fetchone()
    e = telegram.esc
    caption = (f"✅ <b>{e(call['contact'] or 'Звонок')}</b> · {e(fmt_date_full(call['created_at']))} · "
               f"{fmt_duration(call['duration_sec'])}\n"
               f"<i>{e(summarize.KINDS.get(call['kind'], 'Разговор'))}</i>\n\n"
               f"{e(call['short_summary'][:600])}\n\n"
               f"{e(config.BASE_URL)}/calls/{cid}")
    ok = telegram.send_document(call_dir(cid) / "summary.docx", download_name(call, "docx"), caption)
    if not ok:
        ok = telegram.send_message(caption)
    if ok:
        update(cid, notified=1)


def run_forever():
    init()
    with db() as con:
        # То, что обрабатывалось в момент остановки, — снова в очередь
        con.execute("UPDATE calls SET status = 'queued', message = 'Продолжу после перезапуска' "
                    "WHERE status = 'processing'")
    log("Очередь запущена")
    while True:
        call = take_next()
        if not call:
            time.sleep(POLL_SEC)
            continue
        started = time.time()
        log(f"→ {call['id']}: {call['original_name']}")
        try:
            process(call)
            log(f"✓ {call['id']} за {int(time.time() - started)} с")
        except Exception as e:  # noqa: BLE001 — одна запись не должна останавливать очередь
            traceback.print_exc()
            msg = str(e) if isinstance(e, transcribe.GeminiError) else f"{type(e).__name__}: {e}"
            update(call["id"], status="error", error=msg[:500], message="Ошибка")
            telegram.send_message(f"⚠️ Не получилось обработать звонок "
                                  f"<b>{telegram.esc(call['contact'] or call['original_name'])}</b>:\n"
                                  f"{telegram.esc(msg[:300])}\n{telegram.esc(config.BASE_URL)}/calls/{call['id']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8")
    run_forever()
