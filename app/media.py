"""Проверка и перекодирование загруженных записей. ffmpeg всегда запускается
списком аргументов, без shell: имя файла никак не может стать командой."""
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def safe_ext(filename: str) -> str:
    """Расширение только для удобства (ffmpeg смотрит на содержимое, а не на имя)."""
    ext = Path(filename or "").suffix.lower().lstrip(".")
    return ext if re.fullmatch(r"[a-z0-9]{1,5}", ext) else "bin"


def probe(path: Path) -> tuple[bool, int]:
    """(есть ли звуковая дорожка, длительность в секундах)."""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=60).stderr
    except subprocess.TimeoutExpired:
        return False, 0
    has_audio = re.search(r"Stream #\S+.*: Audio:", out) is not None
    m = re.search(r"Duration: (\d+):(\d+):(\d+)", out)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3]) if m else 0
    return has_audio, duration


def to_m4a(src: Path, dst: Path):
    """Копия для прослушивания: AAC моно 64 кбит/с, индекс в начале файла —
    Safari начинает играть сразу и перематывает без загрузки всего файла."""
    tmp = dst.with_suffix(".part.m4a")
    subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-vn", "-ac", "1", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(tmp)],
        check=True, timeout=1800)
    tmp.replace(dst)
