"""Дословная расшифровка записи разговора через Gemini API (бесплатный уровень, модели Flash).

Запуск:
    python transcribe.py "D:\\Downloads\\Голос 002.m4a" ["Голос 003.m4a" ...] [--context "Иван — заказчик"] [--name "Созвон"]

Несколько файлов = один разговор, записанный частями: текст склеивается, время идёт сквозное.

Ключ берётся из переменной окружения GEMINI_API_KEY.
Модели — из GEMINI_MODEL через запятую (по умолчанию gemini-3-flash-preview, gemini-3.5-flash, gemini-3.5-flash-lite).
GEMINI_BASE_URL — адрес своего прокси на Cloudflare Workers (если Google блокирует ваш IP).
GEMINI_PROXY — локальный HTTP-прокси, например http://127.0.0.1:10809.
Результат: results/<имя>/транскрипция.txt

Сайт вызывает transcribe_files() напрямую — см. app/worker.py.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from envfile import load_env_file

load_env_file()

if os.environ.get("GEMINI_PROXY"):
    os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = os.environ["GEMINI_PROXY"]

try:
    from google import genai
    from google.genai import errors, types
    import httpx
    import imageio_ffmpeg
except ImportError:
    sys.exit("Не хватает библиотек. Выполните: python -m pip install -r requirements.txt")

# Модели по порядку: если первая перегружена (503) или кончился лимит (429), берём следующую.
MODELS = [m.strip() for m in os.environ.get(
    "GEMINI_MODEL", "gemini-3-flash-preview,gemini-3.5-flash,gemini-3.5-flash-lite").split(",") if m.strip()]
RESULTS = Path(__file__).resolve().parent / "results"

PROMPT = """Сделай дословную транскрипцию этого фрагмента аудиозаписи делового разговора на русском языке.

Правила:
- Ничего не сокращай, не пересказывай и не обобщай. Нужен каждый сказанный фрагмент.
- Убирай только слова-паразиты и звуки вроде «э-э», «м-м».
- Раздели по говорящим. Каждая реплика — с новой строки в формате:
  [ММ:СС] Имя или роль: текст
  Время — от начала ЭТОГО фрагмента. Длинную реплику разбивай на абзацы по 30–60 секунд, и КАЖДЫЙ абзац начинай с метки [ММ:СС] и имени говорящего.
- Говорящих называй по имени, если оно прозвучало, иначе по роли («Заказчик», «Исполнитель»), иначе «Спикер 1», «Спикер 2».
- Неразборчивые места помечай [неразборчиво]. Посторонние голоса (кто-то рядом, телефон) помечай как «Другой голос».
- Не добавляй ничего, кроме транскрипции: без заголовков, вступлений и выводов, без markdown-разметки.
"""

CONTEXT_RULE = (
    "\nСправка для правильного написания: {context}\n"
    "Используй её ТОЛЬКО чтобы правильно написать имена и слова, которые действительно звучат в записи, "
    "и чтобы назвать говорящих. Никогда не добавляй слова, названия или фразы, которых нет в аудио.\n"
)


class GeminiError(RuntimeError):
    """Gemini недоступна: блокировка по стране, кончились модели, неверный ключ."""


def ffmpeg_exe() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def duration_sec(src: Path) -> int:
    out = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(src)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stderr
    m = re.search(r"Duration: (\d+):(\d+):(\d+)", out)
    return int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) if m else 0


def split_audio(src: Path, chunk_sec: int, out_dir: Path) -> list[Path]:
    """Режет запись на куски по chunk_sec секунд: mp3 моно 16 кГц 32 кбит/с (~5 МБ на 20 минут).
    Gemini всё равно приводит звук к 16 кГц, а маленький кусок помещается прямо в запрос."""
    subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
         "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
         str(out_dir / "chunk%03d.mp3")],
        check=True,
    )
    return sorted(out_dir.glob("chunk*.mp3"))


def make_client():
    if not os.environ.get("GEMINI_API_KEY"):
        raise GeminiError("Не задана переменная GEMINI_API_KEY (см. README).")
    base_url = os.environ.get("GEMINI_BASE_URL")
    if base_url:
        return genai.Client(http_options=types.HttpOptions(base_url=base_url, timeout=600_000))
    return genai.Client(http_options=types.HttpOptions(timeout=600_000))


def generate(client, model: str, contents: list) -> str:
    """Ответ читается потоком: так весь трафик идёт через GEMINI_BASE_URL и не упирается
    в таймауты прокси. Аудио уходит прямо в запросе, без Files API."""
    stream = client.models.generate_content_stream(model=model, contents=contents)
    return "".join(part.text or "" for part in stream)


def generate_with_fallback(client, contents: list, what: str, log=None) -> tuple[str, str]:
    """Пробует модели по очереди; на каждой — до 3 попыток при временных ошибках."""
    log = log or (lambda msg: print(msg, file=sys.stderr))
    last_error = None
    for model in MODELS:
        for attempt in range(3):
            try:
                return generate(client, model, contents), model
            except (httpx.TransportError, ConnectionError) as e:  # обрыв VPN/прокси
                last_error = e
                if attempt < 2:
                    log(f"  {what}: {model} — сетевая ошибка, повтор через 10 с…")
                    time.sleep(10)
                continue
            except errors.APIError as e:
                code = getattr(e, "code", None)
                if code == 400 and "location is not supported" in str(e):
                    raise GeminiError("Google видит российский IP. Задайте GEMINI_BASE_URL "
                                      "(прокси на Cloudflare, см. README).") from e
                if code not in (429, 500, 502, 503, 504, 524):
                    raise
                last_error = e
                if attempt < 2:
                    delay = 60 if code == 429 else 15 * (attempt + 1)
                    log(f"  {what}: {model} — ошибка {code}, повтор через {delay} с…")
                    time.sleep(delay)
        log(f"  {what}: {model} недоступна, пробую следующую модель")
    raise GeminiError(f"Ни одна модель не ответила: {last_error}")


def fmt(sec: int) -> str:
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def shift_timestamps(text: str, offset_sec: int) -> str:
    """[ММ:СС] относительно куска -> [Ч:ММ:СС] от начала всей записи."""
    def repl(m):
        parts = [int(p) for p in m.group(1).split(":")]
        sec = parts[-1] + parts[-2] * 60 + (parts[-3] * 3600 if len(parts) == 3 else 0)
        return f"[{fmt(sec + offset_sec)}]"
    return re.sub(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]", repl, text)


def transcribe_files(sources: list[Path], cache_dir: Path, context: str = "", chunk_min: int = 10,
                     progress=None, log=None) -> tuple[str, int, list[str]]:
    """Расшифровывает файлы одного разговора по порядку.

    Возвращает (текст со сквозными таймкодами, длительность в секундах, использованные модели).
    Готовые куски лежат в cache_dir: при сбое повторный вызов продолжит с места обрыва.
    progress(done, total, сообщение) вызывается перед каждым куском и после последнего.
    """
    log = log or print
    progress = progress or (lambda done, total, msg: None)
    cache_dir.mkdir(parents=True, exist_ok=True)
    client = make_client()
    chunk_sec = chunk_min * 60
    pieces, prev_tail, used = [], "", []
    file_offset = 0
    total = sum(max(1, -(-duration_sec(s) // chunk_sec)) for s in sources)
    done = 0

    for src in sources:
        if len(sources) > 1:
            pieces.append(f"--- {src.name} (с {fmt(file_offset)}) ---")
        with tempfile.TemporaryDirectory() as tmp:
            chunks = split_audio(src, chunk_sec, Path(tmp))
            for i, chunk in enumerate(chunks):
                what = f"{src.name}, часть {i + 1}/{len(chunks)}"
                progress(done, max(total, done + 1), f"Расшифровка: часть {done + 1} из {max(total, done + 1)}")
                log(what + "…")
                prompt = PROMPT
                if context:
                    prompt += CONTEXT_RULE.format(context=context)
                if prev_tail:
                    prompt += ("\nКонец предыдущего фрагмента — используй те же обозначения говорящих:\n"
                               f"{prev_tail}\n")
                cached = cache_dir / f"{src.stem}_{chunk_min}m_{i:03d}.txt"
                if cached.exists():
                    model, _, text = cached.read_text(encoding="utf-8").partition("\n")
                    log(f"  взято из сохранённого ({model})")
                else:
                    audio = types.Part.from_bytes(data=chunk.read_bytes(), mime_type="audio/mpeg")
                    text, model = generate_with_fallback(client, [prompt, audio], what, log)
                    text = text.strip() or "[пусто — модель не вернула текст для этой части]"
                    cached.write_text(f"{model}\n{text}", encoding="utf-8")
                    log(f"  готово ({model})")
                used.append(model)
                prev_tail = "\n".join(text.splitlines()[-6:])
                pieces.append(shift_timestamps(text, file_offset + i * chunk_sec))
                done += 1
        file_offset += duration_sec(src)

    progress(done, done, "Расшифровка готова")
    return "\n\n".join(pieces) + "\n", file_offset, list(dict.fromkeys(used))


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="+", help="Один или несколько файлов одного разговора по порядку")
    ap.add_argument("--context", default="",
                    help="Участники и редкие термины, например: «Иван — заказчик; Исполнитель — разработчик сайта»")
    ap.add_argument("--chunk-min", type=int, default=10)
    ap.add_argument("--name", help="Имя папки результата (по умолчанию — имя первого файла)")
    ap.add_argument("--out-dir", help="Папка результата целиком (вместо results/<имя>)")
    args = ap.parse_args()

    sources = [Path(a).resolve() for a in args.audio]
    for src in sources:
        if not src.exists():
            sys.exit(f"Файл не найден: {src}")

    out_dir = Path(args.out_dir) if args.out_dir else RESULTS / (args.name or sources[0].stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "транскрипция.txt"
    cache_dir = out_dir / ".куски"

    started = time.time()
    print(f"Модели: {', '.join(MODELS)}. Файлов: {len(sources)}, куски по {args.chunk_min} мин.")
    try:
        text, duration, used = transcribe_files(sources, cache_dir, args.context, args.chunk_min)
    except GeminiError as e:
        sys.exit(str(e))

    header = (f"Транскрипция: {', '.join(s.name for s in sources)}\n"
              f"Длительность: {fmt(duration)} · Модель: {', '.join(used)} · "
              f"Расшифровано: {time.strftime('%Y-%m-%d %H:%M')}\n"
              + "=" * 60 + "\n\n")
    out_path.write_text(header + text, encoding="utf-8")
    shutil.rmtree(cache_dir, ignore_errors=True)
    print(f"Готово за {int(time.time() - started)} с: {out_path}")


if __name__ == "__main__":
    main()
