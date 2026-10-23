"""Оформление для страниц: даты, длительность, безопасный markdown → HTML,
разбор расшифровки на реплики для плеера."""
import html
import re
import time
from datetime import datetime

from . import config

MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

STATUS_LABELS = {
    "queued": "В очереди",
    "processing": "Обработка",
    "done": "Готово",
    "error": "Ошибка",
}


def fmt_duration(sec: int) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def local_dt(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, config.TZ)


def fmt_dt(ts: int) -> str:
    dt = local_dt(ts)
    today = local_dt(int(time.time())).date()
    hm = dt.strftime("%H:%M")
    days = (today - dt.date()).days
    if days == 0:
        return f"Сегодня, {hm}"
    if days == 1:
        return f"Вчера, {hm}"
    year = f" {dt.year}" if dt.year != today.year else ""
    return f"{dt.day} {MONTHS[dt.month - 1]}{year}, {hm}"


def fmt_date_full(ts: int) -> str:
    dt = local_dt(ts)
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')}"


def call_title(call) -> str:
    return call["contact"] or "Без имени"


def download_name(call, what: str) -> str:
    """Имя файла при скачивании: «Итог — Иван — 2026-10-24 14-05.docx»."""
    who = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", call["contact"] or "звонок").strip()[:60] or "звонок"
    stamp = local_dt(call["created_at"]).strftime("%Y-%m-%d %H-%M")
    label, ext = {
        "docx": ("Итог", "docx"),
        "txt": ("Расшифровка", "txt"),
        "audio": ("Запись", "m4a"),
    }[what]
    return f"{label} — {who} — {stamp}.{ext}"


# ── markdown итога → HTML ────────────────────────────────────────────────────
# Всё содержимое сначала экранируется, потом размечается. Своего HTML модель
# вставить не может: любые < > & превращаются в текст.

STAMP = re.compile(r"\[(\d{1,2}):(\d{2}):(\d{2})\]")


def _inline(text: str) -> str:
    out = html.escape(text, quote=True)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)

    def stamp(m):
        sec = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        return f'<button type="button" class="ts" data-t="{sec}">{m[1]}:{m[2]}:{m[3]}</button>'
    return STAMP.sub(stamp, out)


def markdown_to_html(md: str) -> str:
    out, list_tag, table = [], None, []

    def close_list():
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_table():
        rows = [r for r in table if not re.fullmatch(r"\|?[\s:|-]+\|?", r.strip())]
        if rows:
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            head, body = cells[0], cells[1:]
            out.append('<div class="table-wrap"><table><thead><tr>'
                       + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr></thead><tbody>"
                       + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in body)
                       + "</tbody></table></div>")
        table.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            close_list()
            table.append(line)
            continue
        if table:
            flush_table()
        if not line.strip():
            close_list()
            continue
        if line.startswith("### "):
            close_list()
            out.append(f"<h3>{_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            close_list()
            out.append(f"<h2>{_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            close_list()
            out.append(f"<h2>{_inline(line[2:])}</h2>")
        elif m := re.match(r"^\s*[-*] (.*)", line):
            if list_tag != "ul":
                close_list()
                out.append("<ul>")
                list_tag = "ul"
            nested = ' class="nested"' if line.startswith("  ") else ""
            out.append(f"<li{nested}>{_inline(m[1])}</li>")
        elif m := re.match(r"^\s*\d+[.)] (.*)", line):
            if list_tag != "ol":
                close_list()
                out.append("<ol>")
                list_tag = "ol"
            out.append(f"<li>{_inline(m[1])}</li>")
        elif line.startswith(">"):
            close_list()
            out.append(f"<blockquote>{_inline(line.lstrip('> ').strip())}</blockquote>")
        elif line.startswith("_") and line.endswith("_") and len(line) > 2:
            close_list()
            out.append(f'<p class="muted">{_inline(line.strip("_"))}</p>')
        else:
            close_list()
            out.append(f"<p>{_inline(line)}</p>")
    if table:
        flush_table()
    close_list()
    return "\n".join(out)


# ── расшифровка → реплики ───────────────────────────────────────────────────

LINE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]\s*([^:]{1,60}):\s*(.*)$")


def parse_transcript(text: str) -> list[dict]:
    speakers: dict[str, int] = {}
    lines = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = LINE.match(raw)
        if not m:
            lines.append({"t": None, "stamp": "", "speaker": "", "color": 0, "text": raw})
            continue
        if m[3] is not None:
            sec = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        else:
            sec = int(m[1]) * 60 + int(m[2])
        speaker = m[4].strip()
        color = speakers.setdefault(speaker, len(speakers) % 4 + 1)
        lines.append({"t": sec, "stamp": fmt_duration(sec), "speaker": speaker, "color": color, "text": m[5]})
    return lines
