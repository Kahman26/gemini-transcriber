"""Вход, сессии, CSRF, защита от перебора, второй фактор (TOTP).

Перебор паролей держат четыре слоя:
1. здесь — пауза для IP, растущая с каждой неудачей, и общий лимит на все адреса сразу;
2. nginx — limit_req на /login;
3. fail2ban — банит IP по строкам LOGIN_FAIL в журнале сервиса;
4. Telegram — сообщение владельцу о блокировках и о каждом успешном входе.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import sys
import time

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import config
from .db import db, del_setting, get_setting, now, set_setting

_hasher = PasswordHasher()
# Проверяем хэш даже при неверном логине — иначе по времени ответа видно, что логин не тот
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

# Перебор с одного адреса: после FREE_FAILS неудач за час каждая следующая
# удваивает паузу: 1, 2, 4, 8… минут, но не больше суток.
FREE_FAILS = 5
# Перебор со многих адресов: столько неудач за час на все адреса — вход паролем
# закрывается на GLOBAL_LOCK_SEC. Открытые сессии при этом продолжают работать.
GLOBAL_FAILS = 30
GLOBAL_LOCK_SEC = 30 * 60
TOTP_TRIES = 5
PENDING_TTL = 5 * 60


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def log_security(event: str, ip: str, extra: str = ""):
    # Строка читается fail2ban (deploy/fail2ban/transcriber.conf) — формат не менять
    print(f"{event} ip={ip} {extra}".strip(), file=sys.stderr, flush=True)


# ── пароль ───────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def check_credentials(login: str, password: str) -> bool:
    stored = config.PASSWORD_HASH
    login_ok = hmac.compare_digest(login.strip().encode(), config.LOGIN.encode())
    try:
        _hasher.verify(stored if (stored and login_ok) else _DUMMY_HASH, password)
        return bool(stored) and login_ok
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ── перебор ──────────────────────────────────────────────────────────────────

def record_attempt(ip: str, kind: str, ok: bool):
    with db() as con:
        con.execute("INSERT INTO attempts(ts, ip, kind, ok) VALUES(?, ?, ?, ?)", (now(), ip, kind, int(ok)))
        # Старое не нужно: хватает суток
        con.execute("DELETE FROM attempts WHERE ts < ?", (now() - 86400,))


def lockout_left(ip: str, kind: str = "password") -> int:
    """Сколько секунд ещё ждать этому адресу. 0 — можно пробовать."""
    t = now()
    with db() as con:
        rows = con.execute(
            "SELECT ts, ok FROM attempts WHERE ip = ? AND kind = ? AND ts > ? ORDER BY ts",
            (ip, kind, t - 3600)).fetchall()
        global_fails = con.execute(
            "SELECT COUNT(*), MAX(ts) FROM attempts WHERE kind = ? AND ok = 0 AND ts > ?",
            (kind, t - 3600)).fetchone()
    # Успешный вход обнуляет счёт этого адреса
    fails = []
    for r in rows:
        fails = [] if r["ok"] else fails + [r["ts"]]
    left = 0
    if len(fails) >= FREE_FAILS:
        pause = min(60 * 2 ** (len(fails) - FREE_FAILS), 86400)
        left = max(left, fails[-1] + pause - t)
    if kind == "password" and global_fails[0] >= GLOBAL_FAILS:
        left = max(left, global_fails[1] + GLOBAL_LOCK_SEC - t)
    return max(0, left)


def fails_in_row(ip: str, kind: str = "password") -> int:
    with db() as con:
        rows = con.execute("SELECT ok FROM attempts WHERE ip = ? AND kind = ? AND ts > ? ORDER BY ts",
                           (ip, kind, now() - 3600)).fetchall()
    n = 0
    for r in rows:
        n = 0 if r["ok"] else n + 1
    return n


def global_fails() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) FROM attempts WHERE kind = 'password' AND ok = 0 AND ts > ?",
                           (now() - 3600,)).fetchone()[0]


# ── сессии ───────────────────────────────────────────────────────────────────

def create_session(kind: str, ip: str, user_agent: str) -> tuple[str, str]:
    """Возвращает (токен для cookie, csrf). В базе — только хэш токена."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    t = now()
    ttl = config.SESSION_DAYS * 86400 if kind == "full" else PENDING_TTL
    with db() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (t,))
        con.execute(
            "INSERT INTO sessions(token_hash, kind, created_at, expires_at, last_seen, ip, user_agent, csrf) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (token_hash(token), kind, t, t + ttl, t, ip, user_agent[:300], csrf))
    return token, csrf


def get_session(token: str | None, kind: str = "full"):
    if not token or len(token) > 100:
        return None
    t = now()
    with db() as con:
        row = con.execute("SELECT * FROM sessions WHERE token_hash = ? AND kind = ? AND expires_at > ?",
                          (token_hash(token), kind, t)).fetchone()
        if row and kind == "full" and t - row["last_seen"] > 3600:
            # Скользящий срок: кто пользуется сайтом, того не выкидывает на вход
            con.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                        (t, t + config.SESSION_DAYS * 86400, row["token_hash"]))
    return row


def delete_session(token: str | None):
    if token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))


def delete_all_sessions():
    with db() as con:
        con.execute("DELETE FROM sessions")
        con.execute("DELETE FROM devices")


def bump_pending_tries(token: str) -> int:
    with db() as con:
        con.execute("UPDATE sessions SET tries = tries + 1 WHERE token_hash = ?", (token_hash(token),))
        row = con.execute("SELECT tries FROM sessions WHERE token_hash = ?", (token_hash(token),)).fetchone()
    return row["tries"] if row else TOTP_TRIES


def check_csrf(session, supplied: str | None) -> bool:
    return bool(session and supplied) and hmac.compare_digest(session["csrf"].encode(), supplied.encode())


# ── запомненные устройства (не спрашивать код второго фактора) ──────────────

def create_device(user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    t = now()
    with db() as con:
        con.execute("DELETE FROM devices WHERE expires_at < ?", (t,))
        con.execute("INSERT INTO devices(token_hash, created_at, expires_at, user_agent) VALUES(?, ?, ?, ?)",
                    (token_hash(token), t, t + config.TRUSTED_DEVICE_DAYS * 86400, user_agent[:300]))
    return token


def device_trusted(token: str | None) -> bool:
    if not token or len(token) > 100:
        return False
    with db() as con:
        return con.execute("SELECT 1 FROM devices WHERE token_hash = ? AND expires_at > ?",
                           (token_hash(token), now())).fetchone() is not None


# ── TOTP (RFC 6238): 6 цифр, шаг 30 секунд ──────────────────────────────────

def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_verify(secret: str, code: str) -> bool:
    """Код текущего шага или соседних (часы телефона могут отставать). Один код — один вход."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6 or not secret:
        return False
    counter = int(time.time()) // 30
    last_used = int(get_setting("totp_last_counter", "0") or 0)
    for c in (counter - 1, counter, counter + 1):
        if c > last_used and hmac.compare_digest(_totp_at(secret, c), code):
            set_setting("totp_last_counter", str(c))
            return True
    return False


def totp_uri(secret: str) -> str:
    return f"otpauth://totp/Transcriber:{config.LOGIN}?secret={secret}&issuer=Transcriber&digits=6&period=30"


def totp_enabled() -> bool:
    return get_setting("totp_enabled") == "1" and bool(get_setting("totp_secret"))


def totp_disable():
    for key in ("totp_enabled", "totp_secret", "totp_pending", "totp_last_counter"):
        del_setting(key)
    with db() as con:
        con.execute("DELETE FROM devices")
