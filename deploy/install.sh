#!/usr/bin/env bash
# Разовая настройка сервера под «Ракету» (задача A-10). Запускать от root:
#
#   DOMAIN=atl73.ru SERVER_IP=195.208.127.167 \
#     bash /opt/raketa/deploy/install.sh /tmp/github-deploy.pub
#
# Повторный запуск безопасен: всё здесь идемпотентно. Единственный довод
# за скрипт вместо списка команд в инструкции — сервер придётся
# разворачивать второй раз, и тогда важно, чтобы он получился такой же.
#
# Чего этот скрипт НЕ делает:
#
# * **Не включает ufw.** Публикация портов в Docker обходит ufw: правило
#   «всё закрыто» не действует на опубликованный порт, и фаервол дал бы
#   ложное спокойствие. Вместо него порт интерфейса привязан к 127.0.0.1
#   (WEB_BIND в .env) — наружу смотрят только 22, 80 и 443.
#
# * **Не создаёт .env.** В нём пароль базы и ключ подписи; их генерирует
#   тот, кто разворачивает, и больше никто их не видит.
set -euo pipefail

DOMAIN=${DOMAIN:-atl73.ru}
EXTRA_NAMES=${EXTRA_NAMES:-www.$DOMAIN}
SERVER_IP=${SERVER_IP:-}
APP_DIR=${APP_DIR:-/opt/raketa}
UPSTREAM=${UPSTREAM:-127.0.0.1:8080}
DEPLOY_USER=${DEPLOY_USER:-deploy}
ACME_ROOT=${ACME_ROOT:-/var/www/acme}
PUBKEY=${1:-}

say() { printf '\n== %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }

say "пакеты"
need=()
for p in nginx certbot dnsutils curl; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
if [ ${#need[@]} -gt 0 ]; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}"
fi
echo "на месте: nginx $(nginx -v 2>&1 | sed 's/.*\///'), certbot $(certbot --version 2>&1 | awk '{print $2}')"

say "каталоги"
install -d -m 0755 /etc/raketa "$ACME_ROOT"
# Пустой файл, а не отсутствующий: конфиг nginx его include-ит безусловно,
# и отсутствие файла — отказ запуска nginx.
[ -f /etc/raketa/hsts.conf ] || : > /etc/raketa/hsts.conf

say "самоподписанный сертификат на время ожидания домена"
self=/etc/raketa/self-signed
install -d -m 0700 "$self"
if [ ! -s "$self/fullchain.pem" ]; then
    alt="DNS:$DOMAIN"
    for n in $EXTRA_NAMES; do alt="$alt,DNS:$n"; done
    [ -n "$SERVER_IP" ] && alt="$alt,IP:$SERVER_IP"
    openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
        -keyout "$self/privkey.pem" -out "$self/fullchain.pem" \
        -subj "/CN=$DOMAIN" -addext "subjectAltName=$alt" 2>/dev/null
    chmod 0600 "$self/privkey.pem"
    echo "создан (браузер будет ругаться — так и должно быть до настоящего)"
else
    echo "уже был"
fi
# Ссылку переставляет cert.sh, когда появится настоящий сертификат.
[ -L /etc/raketa/cert ] || ln -sfn "$self" /etc/raketa/cert
echo "/etc/raketa/cert → $(readlink -f /etc/raketa/cert)"

say "сайт в nginx"
names="$DOMAIN $EXTRA_NAMES"
# Адрес в server_name — чтобы сайт открывался по IP, пока домен не переехал.
# Отдельным default_server его сделать нельзя: 80-й уже занят сайтом хостера,
# а 443-й — панелью Portainer.
[ -n "$SERVER_IP" ] && names="$names $SERVER_IP"
sed -e "s|__NAMES__|$names|g" -e "s|__UPSTREAM__|$UPSTREAM|g" \
    "$APP_DIR/deploy/nginx-site.conf" > /etc/nginx/conf.d/raketa.conf
nginx -t
systemctl reload nginx
echo "имена: $names → $UPSTREAM"

say "команды"
install -m 0755 "$APP_DIR/deploy/deploy.sh" /usr/local/bin/raketa-deploy
install -m 0755 "$APP_DIR/deploy/cert.sh" /usr/local/bin/raketa-cert
# raketa-deploy работает из /usr/local/bin именно потому, что git reset
# в нём перезаписывает исходный файл скрипта — см. комментарий в deploy.sh.
chown "$DEPLOY_USER" /usr/local/bin/raketa-deploy
echo "raketa-deploy, raketa-cert"

say "ожидание домена"
cat > /etc/systemd/system/raketa-cert.service <<CONF
[Unit]
Description=Выдать сертификат, как только домен укажет на эту машину
After=network-online.target nginx.service

[Service]
Type=oneshot
Environment=DOMAIN=$DOMAIN
Environment=EXTRA_NAMES=$EXTRA_NAMES
ExecStart=/usr/local/bin/raketa-cert
CONF
cat > /etc/systemd/system/raketa-cert.timer <<'CONF'
[Unit]
Description=Проверять A-запись раз в час, пока сертификата нет

[Timer]
OnBootSec=3min
OnUnitActiveSec=1h
Unit=raketa-cert.service

[Install]
WantedBy=timers.target
CONF
systemctl daemon-reload
# Таймер выключит сам себя, когда сертификат будет получен.
if [ -s "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then
    echo "сертификат уже есть — таймер не нужен"
else
    systemctl enable --now raketa-cert.timer
    systemctl list-timers raketa-cert.timer --no-pager | head -3
fi

if [ -n "$PUBKEY" ] && [ -f "$PUBKEY" ]; then
    say "ключ для GitHub Actions"
    home=$(getent passwd "$DEPLOY_USER" | cut -d: -f6)
    install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
    keys="$home/.ssh/authorized_keys"
    # Принудительная команда: этим ключом нельзя сделать ничего, кроме
    # развёртывания. Ключ лежит в секретах GitHub, а секреты GitHub читает
    # всякий, кто может менять workflow, — то есть ключ стоит считать
    # известным большему числу людей, чем хотелось бы.
    # Файл перезаписывается целиком, а не дополняется: у этого пользователя
    # одна задача, и список его ключей должен быть обозримым. Лишний ключ,
    # забытый в authorized_keys, — это доступ, о котором никто не помнит.
    opts='restrict,command="/usr/local/bin/raketa-deploy"'
    printf '%s %s\n' "$opts" "$(cat "$PUBKEY")" > "$keys"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$keys"
    chmod 0600 "$keys"
    echo "ключ записан с принудительной командой raketa-deploy"
fi

say "итог"
echo "следующий шаг — .env и первый запуск:"
echo "  sudo -u $DEPLOY_USER -i"
echo "  cd $APP_DIR && cp .env.prod.example .env && \$EDITOR .env"
echo "  raketa-deploy"
