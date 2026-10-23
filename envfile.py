"""Настройки из файлов КЛЮЧ=значение: локальный .env рядом с кодом и серверный
/etc/transcriber.env (если его можно прочитать — например, команды от root).

Службы на сервере получают переменные от systemd, а на своём компьютере удобнее
держать их в файле. Заданное в окружении важнее файла. Подстановок вида $VAR нет
намеренно: в хэше пароля полно знаков доллара — поэтому не делайте `source` этого файла.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"
SERVER_ENV_PATH = Path("/etc/transcriber.env")


def load_env_file() -> None:
    for path in (ENV_PATH, SERVER_ENV_PATH):
        if os.access(path, os.R_OK):
            _load(path)


def _load(path: Path) -> None:
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
