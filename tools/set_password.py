"""Задать пароль для входа на сайт. Пароль вводится скрыто, на диск и на сервер
уходит только хэш argon2id.

Локально (пишет в .env рядом с кодом):
    python tools/set_password.py

Сразу на сервер (пишет в /etc/transcriber.env и перезапускает сайт):
    python tools/set_password.py --server root@130.49.146.66 --key C:\\путь\\к\\ssh-ключу
"""
import argparse
import getpass
import re
import subprocess
import sys
from pathlib import Path

from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parent.parent
REMOTE_ENV = "/etc/transcriber.env"

# Программа для сервера уходит через stdin, а не аргументом команды: так хэш
# не мелькает в списке процессов.
REMOTE_SCRIPT = r'''
import os, re
path = {path!r}
line = "TRANSCRIBER_PASSWORD_HASH=" + {hash!r}
text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
if re.search(r"^TRANSCRIBER_PASSWORD_HASH=.*$", text, flags=re.M):
    text = re.sub(r"^TRANSCRIBER_PASSWORD_HASH=.*$", lambda m: line, text, flags=re.M)
else:
    text = text.rstrip("\n") + "\n" + line + "\n"
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(text)
os.chmod(path, 0o600)
print("Пароль обновлён в", path)
'''


def ask_password() -> str:
    while True:
        first = getpass.getpass("Новый пароль (не меньше 12 символов): ")
        if len(first) < 12:
            print("Слишком короткий.")
            continue
        if getpass.getpass("Ещё раз: ") != first:
            print("Не совпадает, попробуйте снова.")
            continue
        return first


def write_local(hashed: str):
    env = ROOT / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    line = f"TRANSCRIBER_PASSWORD_HASH={hashed}"
    if re.search(r"^TRANSCRIBER_PASSWORD_HASH=.*$", text, flags=re.M):
        text = re.sub(r"^TRANSCRIBER_PASSWORD_HASH=.*$", lambda m: line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + ("\n" if text else "") + line + "\n"
    env.write_text(text, encoding="utf-8")
    print(f"Записано в {env}. Перезапустите сайт.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", help="root@адрес — записать сразу на сервер")
    ap.add_argument("--key", help="SSH-ключ для сервера")
    args = ap.parse_args()

    hashed = PasswordHasher().hash(ask_password())
    if not args.server:
        write_local(hashed)
        return

    ssh = ["ssh", "-o", "IdentitiesOnly=yes"] + (["-i", args.key] if args.key else []) + [args.server]
    script = REMOTE_SCRIPT.format(path=REMOTE_ENV, hash=hashed)
    result = subprocess.run(ssh + ["python3 - && systemctl restart transcriber"],
                            input=script, text=True, encoding="utf-8")
    if result.returncode:
        sys.exit("Не получилось записать пароль на сервер (см. ошибку выше).")
    print("Готово: сайт перезапущен с новым паролем.")


if __name__ == "__main__":
    main()
