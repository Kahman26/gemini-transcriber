#!/usr/bin/env bash
# Первичная настройка сервера под транскрибер. Запускается один раз, от root:
#   ssh root@сервер 'bash -s' < deploy/bootstrap.sh
# Повторный запуск безопасен: всё, что уже сделано, пропускается. Если DNS ещё
# не указывает на сервер, сертификат и nginx пропускаются — запустите позже снова.
#
# Дальше обновления идут сами через GitHub Actions → deploy/deploy.sh.

set -euo pipefail

REPO=https://github.com/Kahman26/gemini-transcriber.git
DOMAIN=transcriber.kahman.studio
EMAIL=andrey.ivanov53563@gmail.com
APP_DIR=/opt/transcriber
DATA_DIR=/var/lib/transcriber
APP_USER=transcriber
ENV_FILE=/etc/transcriber.env

echo "→ Пакеты"
apt-get update -qq
apt-get install -y -qq python3-venv git curl fail2ban >/dev/null

echo "→ Системный пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "→ Каталоги"
mkdir -p "$APP_DIR" "$DATA_DIR" "/var/www/$DOMAIN"
chown "$APP_USER:$APP_USER" "$DATA_DIR"
chmod 700 "$DATA_DIR"

echo "→ Код"
if [ ! -d "$APP_DIR/.git" ]; then
    git clone -q "$REPO" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
git config --global --add safe.directory "$APP_DIR"

echo "→ Виртуальное окружение"
[ -d "$APP_DIR/venv" ] || sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "→ Настройки $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
    # Только несекретное. Ключи дописываются отдельно (см. README), пароль —
    # командой tools/set_password.py.
    install -m 600 -o root -g root /dev/null "$ENV_FILE"
    cat > "$ENV_FILE" <<ENV
TRANSCRIBER_BASE_URL=https://$DOMAIN
TRANSCRIBER_DATA_DIR=$DATA_DIR
TRANSCRIBER_LOGIN=arkprhrv
TRANSCRIBER_PASSWORD_HASH=
TRANSCRIBER_TZ=Asia/Yekaterinburg
GEMINI_API_KEY=
GEMINI_BASE_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_API_BASE=
TELEGRAM_PROXY_SECRET=
ENV
fi
chmod 600 "$ENV_FILE"

echo "→ systemd"
cp "$APP_DIR/deploy/transcriber.service" /etc/systemd/system/transcriber.service
cp "$APP_DIR/deploy/transcriber-worker.service" /etc/systemd/system/transcriber-worker.service
systemctl daemon-reload
systemctl enable --now transcriber transcriber-worker

echo "→ fail2ban"
cp "$APP_DIR/deploy/fail2ban/filter.conf" /etc/fail2ban/filter.d/transcriber.conf
cp "$APP_DIR/deploy/fail2ban/jail.conf" /etc/fail2ban/jail.d/transcriber.conf
systemctl enable --now fail2ban >/dev/null 2>&1 || true
fail2ban-client reload >/dev/null 2>&1 || systemctl restart fail2ban

echo "→ Сертификат"
SERVER_IPS=" $(hostname -I) "
DNS_IP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1 {print $1}' || true)
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    if [ -z "$DNS_IP" ] || [[ "$SERVER_IPS" != *" $DNS_IP "* ]]; then
        echo "  ✗ $DOMAIN пока указывает на «${DNS_IP:-ничего}», а у сервера адреса:$SERVER_IPS"
        echo "    Сертификат и nginx пропущены. Когда DNS обновится, запустите bootstrap ещё раз."
        exit 0
    fi
    # Временный HTTP-блок: certbot должен куда-то положить проверочный файл,
    # а боевой конфиг без сертификата не поднимется.
    cat > "/etc/nginx/sites-available/$DOMAIN" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    root /var/www/$DOMAIN;
    location ^~ /.well-known/acme-challenge/ { default_type "text/plain"; }
}
NGINX
    ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
    nginx -t && systemctl reload nginx
    certbot certonly --webroot -w "/var/www/$DOMAIN" -d "$DOMAIN" \
        --non-interactive --agree-tos -m "$EMAIL"
fi

echo "→ Боевой конфиг nginx"
cp "$APP_DIR/deploy/nginx-transcriber.conf" "/etc/nginx/sites-available/$DOMAIN"
ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
nginx -t && systemctl reload nginx

echo
echo "Готово: https://$DOMAIN"
systemctl is-active transcriber transcriber-worker
