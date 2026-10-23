#!/usr/bin/env bash
# Обновление транскрибера. Запускается от root.
#
# GitHub Actions не вызывает этот файл на сервере, а передаёт его по stdin:
#   ssh root@host 'bash -s' < deploy/deploy.sh
# Так выполняется всегда свежая версия скрипта, и bash не читает файл,
# который прямо сейчас переписывает git.

set -euo pipefail

APP_DIR=/opt/transcriber
APP_USER=transcriber
BRANCH=main
DOMAIN=transcriber.kahman.studio

cd "$APP_DIR"

echo "→ Забираю изменения из origin/$BRANCH"
sudo -u "$APP_USER" git fetch --prune origin
if ! sudo -u "$APP_USER" git rev-parse --verify -q "origin/$BRANCH" >/dev/null; then
    echo "✗ В origin нет ветки $BRANCH — деплоить нечего. Код на сервере не тронут."
    exit 1
fi
# reset --hard трогает только отслеживаемые файлы: база и записи лежат
# в /var/lib/transcriber, ключи — в /etc/transcriber.env.
sudo -u "$APP_USER" git reset --hard "origin/$BRANCH"

echo "→ Зависимости"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r requirements.txt

echo "→ Службы systemd"
units_changed=0
for unit in transcriber.service transcriber-worker.service; do
    if ! cmp -s "deploy/$unit" "/etc/systemd/system/$unit"; then
        cp "deploy/$unit" "/etc/systemd/system/$unit"
        units_changed=1
    fi
done
[ "$units_changed" = 1 ] && systemctl daemon-reload && echo "  обновлены" || echo "  без изменений"

echo "→ fail2ban"
f2b_changed=0
cmp -s deploy/fail2ban/filter.conf /etc/fail2ban/filter.d/transcriber.conf || {
    cp deploy/fail2ban/filter.conf /etc/fail2ban/filter.d/transcriber.conf; f2b_changed=1; }
cmp -s deploy/fail2ban/jail.conf /etc/fail2ban/jail.d/transcriber.conf || {
    cp deploy/fail2ban/jail.conf /etc/fail2ban/jail.d/transcriber.conf; f2b_changed=1; }
if [ "$f2b_changed" = 1 ]; then
    fail2ban-client reload >/dev/null && echo "  обновлён" || echo "  ✗ fail2ban не перечитал настройки"
else
    echo "  без изменений"
fi

echo "→ Конфиг nginx"
NGINX_SRC="$APP_DIR/deploy/nginx-transcriber.conf"
NGINX_DST="/etc/nginx/sites-available/$DOMAIN"
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo "  сертификата ещё нет — пропускаю (запустите deploy/bootstrap.sh)"
elif ! cmp -s "$NGINX_SRC" "$NGINX_DST"; then
    cp "$NGINX_DST" "/tmp/nginx-$DOMAIN.bak" 2>/dev/null || true
    cp "$NGINX_SRC" "$NGINX_DST"
    if nginx -t 2>/dev/null; then
        systemctl reload nginx
        echo "  обновлён"
    else
        # Ломать соседние сайты (leadgen, основной) на этом nginx нельзя
        cp "/tmp/nginx-$DOMAIN.bak" "$NGINX_DST"
        echo "✗ Новый конфиг nginx не прошёл проверку, вернул прежний:"
        nginx -t || true
        exit 1
    fi
else
    echo "  без изменений"
fi

echo "→ Перезапуск"
systemctl restart transcriber transcriber-worker
for _ in $(seq 1 15); do
    curl -fsS --max-time 2 http://127.0.0.1:8780/healthz >/dev/null 2>&1 && break
    sleep 1
done

if curl -fsS --max-time 3 http://127.0.0.1:8780/healthz >/dev/null && systemctl is-active --quiet transcriber-worker; then
    echo "✓ Готово: $(git log -1 --format='%h %s')"
else
    echo "✗ Служба не поднялась, последние строки журнала:"
    journalctl -u transcriber -u transcriber-worker -n 40 --no-pager
    exit 1
fi
