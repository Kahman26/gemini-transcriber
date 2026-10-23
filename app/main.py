"""Сайт транскрибера: вход, история звонков, загрузка, страница звонка, настройки.

Локально:  python -m uvicorn app.main:app --port 8780   (в .env — TRANSCRIBER_DEV=1)
На сервере — служба transcriber (deploy/transcriber.service) за nginx.
"""
import asyncio
import base64
import io
import re
import secrets
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import segno
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from summarize import KINDS, TEMPLATES

from . import config, render, security, telegram
from .db import db, del_setting, get_setting, init, now, set_setting
from .media import probe, safe_ext

APP_DIR = Path(__file__).resolve().parent
CALL_ID = re.compile(r"^[0-9a-f]{32}$")

init()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[
    urlsplit(config.BASE_URL).hostname or "localhost", "127.0.0.1", "localhost"])
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.globals.update(
    fmt_dt=render.fmt_dt, fmt_duration=render.fmt_duration, fmt_date_full=render.fmt_date_full,
    call_title=render.call_title, STATUS_LABELS=render.STATUS_LABELS, TEMPLATES=TEMPLATES, KINDS=KINDS,
    MAX_UPLOAD_MB=config.MAX_UPLOAD_MB, MAX_FILES=config.MAX_FILES_PER_UPLOAD,
)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "media-src 'self' blob:; connect-src 'self'; font-src 'self'; manifest-src 'self'; "
       "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "same-origin"
    h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    h["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    if not config.DEV:
        h["Strict-Transport-Security"] = "max-age=63072000"
    if not request.url.path.startswith("/static/"):
        h["Cache-Control"] = "no-store"
    return response


# ── помощники ────────────────────────────────────────────────────────────────

def client_ip(request: Request) -> str:
    # uvicorn запущен с --proxy-headers и доверяет только 127.0.0.1 (nginx),
    # поэтому подделать адрес заголовком X-Forwarded-For снаружи нельзя.
    return request.client.host if request.client else "?"


def same_origin(request: Request) -> bool:
    """Защита от CSRF вторым слоем: POST принимаем только со своих страниц."""
    allowed = {config.BASE_URL}
    if config.DEV:
        allowed.add(f"{request.url.scheme}://{request.headers.get('host', '')}")
    origin = request.headers.get("origin")
    if origin:
        return origin in allowed
    referer = request.headers.get("referer", "")
    return any(referer == a or referer.startswith(a + "/") for a in allowed)


def set_cookie(response: Response, name: str, value: str, max_age: int):
    response.set_cookie(name, value, max_age=max_age, path="/", httponly=True,
                        secure=not config.DEV, samesite="lax")


def current_session(request: Request):
    return security.get_session(request.cookies.get(config.COOKIE_SESSION))


def to_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def page(request: Request, name: str, session=None, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=session["csrf"] if session else "", logged_in=bool(session))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def notify_async(text: str):
    threading.Thread(target=telegram.send_message, args=(text,), daemon=True).start()


def get_call(call_id: str):
    if not CALL_ID.match(call_id or ""):
        return None
    with db() as con:
        return con.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()


def call_dir(call_id: str) -> Path:
    # call_id уже проверен регуляркой: только 32 hex-символа, выйти из папки нельзя
    return config.CALLS_DIR / call_id


async def check_post(request: Request):
    """Сессия + Origin + CSRF-токен из формы или заголовка. Возвращает (session, form) или (None, None)."""
    session = current_session(request)
    if not session or not same_origin(request):
        return None, None
    form = await request.form()
    supplied = request.headers.get("x-csrf-token") or form.get("csrf")
    if not security.check_csrf(session, supplied if isinstance(supplied, str) else None):
        return None, None
    return session, form


def forbidden() -> PlainTextResponse:
    return PlainTextResponse("Доступ запрещён. Обновите страницу и попробуйте снова.", status_code=403)


# ── служебное ────────────────────────────────────────────────────────────────

@app.get("/healthz", include_in_schema=False)
def healthz():
    return PlainTextResponse("ok")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(APP_DIR / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_icon():
    return FileResponse(APP_DIR / "static" / "icons" / "apple-touch-icon.png")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(APP_DIR / "static" / "icons" / "icon-192.png")


# ── вход ─────────────────────────────────────────────────────────────────────

def login_page(request: Request, error: str = "", status_code: int = 200):
    disabled = not config.PASSWORD_HASH
    return page(request, "login.html", error=error, disabled=disabled, status_code=status_code)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_session(request):
        return RedirectResponse("/", status_code=303)
    return login_page(request)


@app.post("/login")
async def login(request: Request):
    ip = client_ip(request)
    if not same_origin(request):
        return forbidden()
    form = await request.form()
    login_value = str(form.get("login", ""))[:100]
    password = str(form.get("password", ""))[:200]

    wait = await run_in_threadpool(security.lockout_left, ip)
    if wait:
        security.log_security("LOGIN_BLOCKED", ip)
        return login_page(request, f"Слишком много попыток. Попробуйте через {max(1, wait // 60)} мин.", 429)

    ok = await run_in_threadpool(security.check_credentials, login_value, password)
    await run_in_threadpool(security.record_attempt, ip, "password", ok)
    if not ok:
        security.log_security("LOGIN_FAIL", ip)
        fails = await run_in_threadpool(security.fails_in_row, ip)
        if fails == security.FREE_FAILS:
            notify_async(f"🚨 Транскрибер: {fails} неверных паролей подряд с адреса <code>{telegram.esc(ip)}</code>. "
                         "Адрес временно заблокирован.")
        if await run_in_threadpool(security.global_fails) == security.GLOBAL_FAILS:
            notify_async("🚨 Транскрибер: массовый подбор пароля с разных адресов. "
                         "Вход паролем закрыт на 30 минут, открытые сессии работают.")
        await asyncio.sleep(0.4 + secrets.randbelow(400) / 1000)
        return login_page(request, "Неверный логин или пароль.", 401)

    ua = request.headers.get("user-agent", "")
    if security.totp_enabled() and not security.device_trusted(request.cookies.get(config.COOKIE_DEVICE)):
        token, _ = security.create_session("pending", ip, ua)
        response = RedirectResponse("/login/code", status_code=303)
        set_cookie(response, config.COOKIE_PENDING, token, security.PENDING_TTL)
        return response
    return finish_login(request, ip, ua)


def finish_login(request: Request, ip: str, ua: str, remember_device: bool = False) -> Response:
    token, _ = security.create_session("full", ip, ua)
    response = RedirectResponse("/", status_code=303)
    set_cookie(response, config.COOKIE_SESSION, token, config.SESSION_DAYS * 86400)
    response.delete_cookie(config.COOKIE_PENDING, path="/")
    if remember_device:
        set_cookie(response, config.COOKIE_DEVICE, security.create_device(ua), config.TRUSTED_DEVICE_DAYS * 86400)
    security.log_security("LOGIN_OK", ip)
    notify_async(f"🔐 Вход в транскрибер\nАдрес: <code>{telegram.esc(ip)}</code>\n"
                 f"Устройство: {telegram.esc(ua[:150])}")
    return response


@app.get("/login/code", response_class=HTMLResponse)
def code_form(request: Request):
    pending = security.get_session(request.cookies.get(config.COOKIE_PENDING), kind="pending")
    if not pending:
        return to_login()
    return page(request, "code.html", session=pending, error="")


@app.post("/login/code")
async def code_submit(request: Request):
    ip = client_ip(request)
    token = request.cookies.get(config.COOKIE_PENDING)
    pending = security.get_session(token, kind="pending")
    if not pending or not same_origin(request):
        return to_login()
    form = await request.form()
    if not security.check_csrf(pending, str(form.get("csrf", ""))):
        return forbidden()
    if security.lockout_left(ip, "totp"):
        return page(request, "code.html", session=pending, error="Слишком много попыток. Подождите.",
                    status_code=429)

    ok = security.totp_verify(get_setting("totp_secret"), str(form.get("code", ""))[:12])
    security.record_attempt(ip, "totp", ok)
    if not ok:
        security.log_security("TOTP_FAIL", ip)
        if security.bump_pending_tries(token) >= security.TOTP_TRIES:
            security.delete_session(token)
            return login_page(request, "Слишком много неверных кодов. Войдите заново.", 401)
        return page(request, "code.html", session=pending, error="Неверный код.", status_code=401)

    security.delete_session(token)
    return finish_login(request, ip, request.headers.get("user-agent", ""),
                        remember_device=form.get("remember") == "1")


@app.post("/logout")
async def logout(request: Request):
    session, _ = await check_post(request)
    response = to_login()
    if session:
        security.delete_session(request.cookies.get(config.COOKIE_SESSION))
        response.delete_cookie(config.COOKIE_SESSION, path="/")
    return response


# ── история ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    session = current_session(request)
    if not session:
        return to_login()
    with db() as con:
        calls = con.execute("SELECT * FROM calls ORDER BY created_at DESC LIMIT 500").fetchall()
    return page(request, "index.html", session=session, calls=calls)


@app.get("/calls/status")
def calls_status(request: Request, ids: str = ""):
    if not current_session(request):
        return JSONResponse({"error": "auth"}, status_code=401)
    wanted = [i for i in ids.split(",") if CALL_ID.match(i)][:100]
    if not wanted:
        return JSONResponse({})
    with db() as con:
        rows = con.execute(
            f"SELECT id, status, progress, message FROM calls WHERE id IN ({','.join('?' * len(wanted))})",
            wanted).fetchall()
    return JSONResponse({r["id"]: {"status": r["status"], "progress": r["progress"], "message": r["message"],
                                   "label": render.STATUS_LABELS.get(r["status"], r["status"])} for r in rows})


# ── загрузка ─────────────────────────────────────────────────────────────────

@app.get("/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    session = current_session(request)
    if not session:
        return to_login()
    return page(request, "upload.html", session=session)


class UploadError(Exception):
    pass


def save_upload(upload: UploadFile, contact: str, template: str, origin: str) -> str:
    """Сохраняет файл под сгенерированным именем, проверяет, что это звук, ставит в очередь."""
    call_id = uuid.uuid4().hex
    d = call_dir(call_id)
    d.mkdir(parents=True)
    ext = safe_ext(upload.filename or "")
    src = d / f"source.{ext}"
    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    size = 0
    try:
        upload.file.seek(0)
        with src.open("wb") as out:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise UploadError(f"Файл больше {config.MAX_UPLOAD_MB} МБ")
                out.write(chunk)
        if size == 0:
            raise UploadError("Пустой файл")
        has_audio, duration = probe(src)
        if not has_audio:
            raise UploadError("Это не похоже на аудиозапись")
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise

    original = re.sub(r"[\x00-\x1f\x7f]", "", Path(upload.filename or "").name)[:200] or f"запись.{ext}"
    t = now()
    with db() as con:
        con.execute(
            "INSERT INTO calls(id, created_at, updated_at, contact, template, original_name, source_ext, origin, "
            "size_bytes, duration_sec, status, redo, message) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'all', "
            "'В очереди')",
            (call_id, t, t, contact, template, original, ext, origin, size, duration))
    return call_id


def clean_contact(value) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or "")).strip()[:80]


def clean_template(value) -> str:
    value = str(value or "auto")
    return value if value in TEMPLATES else "auto"


async def handle_upload(form, origin: str) -> tuple[list[str], list[str]]:
    files = [f for f in form.getlist("files") + form.getlist("file") if isinstance(f, UploadFile)]
    if not files:
        return [], ["Файл не выбран"]
    if len(files) > config.MAX_FILES_PER_UPLOAD:
        return [], [f"Не больше {config.MAX_FILES_PER_UPLOAD} файлов за раз"]
    contact, template = clean_contact(form.get("contact")), clean_template(form.get("template"))
    ids, errors = [], []
    for f in files:
        try:
            ids.append(await run_in_threadpool(save_upload, f, contact, template, origin))
        except UploadError as e:
            errors.append(f"{f.filename}: {e}")
        finally:
            await f.close()
    return ids, errors


@app.post("/upload")
async def upload(request: Request):
    session, form = await check_post(request)
    if not session:
        return JSONResponse({"ok": False, "errors": ["Сессия истекла — обновите страницу"]}, status_code=403)
    ids, errors = await handle_upload(form, "web")
    return JSONResponse({"ok": bool(ids), "ids": ids, "errors": errors,
                         "redirect": f"/calls/{ids[0]}" if len(ids) == 1 else "/"},
                        status_code=200 if ids else 400)


@app.post("/api/upload")
async def api_upload(request: Request):
    """Для Быстрой команды iPhone: Authorization: Bearer tr_…, поле file (или files), contact, template."""
    ip = client_ip(request)
    if security.lockout_left(ip, "api"):
        return PlainTextResponse("Слишком много неверных попыток, подождите", status_code=429)
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not security.check_api_token(token):
        security.record_attempt(ip, "api", False)
        security.log_security("API_FAIL", ip)
        return PlainTextResponse("Неверный токен", status_code=401)
    form = await request.form()
    ids, errors = await handle_upload(form, "shortcut")
    if not ids:
        return PlainTextResponse("Не принято: " + "; ".join(errors), status_code=400)
    text = "Принято, расшифровка уже идёт" if len(ids) == 1 else f"Принято файлов: {len(ids)}"
    if errors:
        text += ". Не приняты: " + "; ".join(errors)
    return PlainTextResponse(text)


# ── звонок ───────────────────────────────────────────────────────────────────

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


@app.get("/calls/{call_id}", response_class=HTMLResponse)
def call_page(request: Request, call_id: str):
    session = current_session(request)
    if not session:
        return to_login()
    call = get_call(call_id)
    if not call:
        return page(request, "404.html", session=session, status_code=404)
    d = call_dir(call_id)
    transcript = read_text(d / "transcript.txt")
    summary_md = read_text(d / "summary.md")
    return page(request, "call.html", session=session, call=call,
                summary_html=render.markdown_to_html(summary_md) if summary_md else "",
                lines=render.parse_transcript(transcript) if transcript else [],
                has_audio=(d / "audio.m4a").exists(), has_docx=(d / "summary.docx").exists(),
                has_txt=bool(transcript))


FILES = {
    "audio": ("audio.m4a", "audio/mp4"),
    "docx": ("summary.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "txt": ("transcript.txt", "text/plain; charset=utf-8"),
}


@app.get("/calls/{call_id}/file/{what}")
def call_file(request: Request, call_id: str, what: str, download: int = 0):
    """Файлы звонка — только после входа. Range поддерживается (перемотка на iPhone)."""
    if not current_session(request):
        return PlainTextResponse("Нужно войти", status_code=401)
    call = get_call(call_id)
    if not call or what not in FILES:
        return PlainTextResponse("Не найдено", status_code=404)
    name, media_type = FILES[what]
    path = call_dir(call_id) / name
    if not path.exists():
        return PlainTextResponse("Файла пока нет", status_code=404)
    return FileResponse(path, media_type=media_type, filename=render.download_name(call, what),
                        content_disposition_type="attachment" if download else "inline",
                        headers={"Cache-Control": "private, max-age=0, must-revalidate"})


@app.post("/calls/{call_id}/edit")
async def call_edit(request: Request, call_id: str):
    session, form = await check_post(request)
    if not session:
        return forbidden()
    if not get_call(call_id):
        return PlainTextResponse("Не найдено", status_code=404)
    with db() as con:
        con.execute("UPDATE calls SET contact = ?, template = ?, updated_at = ? WHERE id = ?",
                    (clean_contact(form.get("contact")), clean_template(form.get("template")), now(), call_id))
    return RedirectResponse(f"/calls/{call_id}", status_code=303)


@app.post("/calls/{call_id}/redo")
async def call_redo(request: Request, call_id: str):
    session, form = await check_post(request)
    if not session:
        return forbidden()
    call = get_call(call_id)
    if not call:
        return PlainTextResponse("Не найдено", status_code=404)
    if call["status"] in ("queued", "processing"):
        return RedirectResponse(f"/calls/{call_id}", status_code=303)
    mode = "all" if form.get("mode") == "all" else "summary"
    d = call_dir(call_id)
    if mode == "all":
        (d / "transcript.txt").unlink(missing_ok=True)
        shutil.rmtree(d / ".chunks", ignore_errors=True)
    elif not (d / "transcript.txt").exists():
        mode = "all"
    with db() as con:
        con.execute("UPDATE calls SET status = 'queued', redo = ?, progress = 0, message = 'В очереди', "
                    "error = '', updated_at = ? WHERE id = ?", (mode, now(), call_id))
    return RedirectResponse(f"/calls/{call_id}", status_code=303)


@app.post("/calls/{call_id}/delete")
async def call_delete(request: Request, call_id: str):
    session, _ = await check_post(request)
    if not session:
        return forbidden()
    call = get_call(call_id)
    if not call:
        return RedirectResponse("/", status_code=303)
    if call["status"] == "processing":
        return RedirectResponse(f"/calls/{call_id}", status_code=303)
    with db() as con:
        con.execute("DELETE FROM calls WHERE id = ?", (call_id,))
    shutil.rmtree(call_dir(call_id), ignore_errors=True)
    return RedirectResponse("/", status_code=303)


# ── настройки ────────────────────────────────────────────────────────────────

def qr_data_uri(text: str) -> str:
    buf = io.BytesIO()
    segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, dark="#111", light="#fff")
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def settings_page(request: Request, session, **extra):
    totp_pending = get_setting("totp_pending")
    ctx = dict(
        tokens=security.list_api_tokens(),
        totp_on=security.totp_enabled(),
        totp_secret=totp_pending,
        totp_qr=qr_data_uri(security.totp_uri(totp_pending)) if totp_pending else "",
        telegram_on=telegram.enabled(),
        base_url=config.BASE_URL,
        new_token="", message="", error="",
    )
    ctx.update(extra)
    return page(request, "settings.html", session=session, **ctx)


@app.get("/settings", response_class=HTMLResponse)
def settings(request: Request):
    session = current_session(request)
    if not session:
        return to_login()
    return settings_page(request, session)


@app.post("/settings/token")
async def settings_token(request: Request):
    session, form = await check_post(request)
    if not session:
        return forbidden()
    name = clean_contact(form.get("name")) or "iPhone"
    token = security.create_api_token(name)
    # Токен показывается один раз: в базе остаётся только хэш
    return settings_page(request, session, new_token=token, message="Токен создан. Скопируйте его сейчас.")


@app.post("/settings/token/{token_id}/delete")
async def settings_token_delete(request: Request, token_id: int):
    session, _ = await check_post(request)
    if not session:
        return forbidden()
    security.delete_api_token(token_id)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/totp/start")
async def totp_start(request: Request):
    session, _ = await check_post(request)
    if not session:
        return forbidden()
    set_setting("totp_pending", security.totp_new_secret())
    return RedirectResponse("/settings#totp", status_code=303)


@app.post("/settings/totp/confirm")
async def totp_confirm(request: Request):
    session, form = await check_post(request)
    if not session:
        return forbidden()
    secret = get_setting("totp_pending")
    if not secret or not security.totp_verify(secret, str(form.get("code", ""))[:12], remember=False):
        return settings_page(request, session, error="Код не подошёл. Проверьте время на телефоне и попробуйте ещё раз.")
    set_setting("totp_secret", secret)
    set_setting("totp_enabled", "1")
    set_setting("totp_last_counter", "0")
    del_setting("totp_pending")
    response = settings_page(request, session, message="Второй фактор включён. Это устройство запомнено.")
    set_cookie(response, config.COOKIE_DEVICE, security.create_device(request.headers.get("user-agent", "")),
               config.TRUSTED_DEVICE_DAYS * 86400)
    return response


@app.post("/settings/totp/disable")
async def totp_disable(request: Request):
    session, form = await check_post(request)
    if not session:
        return forbidden()
    if security.totp_enabled() and not security.totp_verify(get_setting("totp_secret"),
                                                            str(form.get("code", ""))[:12]):
        return settings_page(request, session, error="Чтобы выключить второй фактор, введите верный код.")
    security.totp_disable()
    return settings_page(request, session, message="Второй фактор выключен.")


@app.post("/settings/telegram/test")
async def telegram_test(request: Request):
    session, _ = await check_post(request)
    if not session:
        return forbidden()
    ok = await run_in_threadpool(telegram.send_message, "✅ Транскрибер на связи")
    return settings_page(request, session, **({"message": "Сообщение отправлено в Telegram."} if ok else
                                              {"error": "Не получилось отправить. Проверьте настройки бота."}))


@app.post("/settings/logout-all")
async def logout_all(request: Request):
    session, _ = await check_post(request)
    if not session:
        return forbidden()
    security.delete_all_sessions()
    response = to_login()
    response.delete_cookie(config.COOKIE_SESSION, path="/")
    response.delete_cookie(config.COOKIE_DEVICE, path="/")
    return response


@app.exception_handler(404)
async def not_found(request: Request, exc):
    session = current_session(request)
    return page(request, "404.html", session=session, status_code=404)
