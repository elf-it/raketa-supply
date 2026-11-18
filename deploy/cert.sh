#!/usr/bin/env bash
# Настоящий сертификат — как только домен начнёт указывать на эту машину
# (задача A-10). Запускается таймером systemd раз в час и руками: raketa-cert.
#
# Решения и почему так:
#
# * **Скрипт сам проверяет A-запись и молча уходит, если она смотрит не сюда.**
#   Let's Encrypt проверяет владение домена, обращаясь к нему по HTTP. Пока
#   A-запись указывает на другой адрес, проверка уйдёт на чужую машину
#   и провалится, а пять таких провалов в час — путь в бан на неделю.
#   Поэтому неподходящая A-запись здесь не ошибка, а причина подождать.
#
# * **Имена берутся те, что действительно указывают сюда.** Один
#   не доехавший www в списке -d губит выдачу целиком, включая основное имя.
#
# * **Пока сертификата нет, сайт работает по самоподписанному.** Иначе
#   COOKIE_SECURE=true не даст войти вообще: приложение намеренно не стартует
#   без HTTPS вне отладки. Браузер ругается, вход работает.
#
# * **HSTS включается только с настоящим сертификатом.** Этот заголовок
#   запрещает браузеру «пройти всё равно» — с самоподписанным он закрыл бы
#   сайт насовсем.
set -euo pipefail

DOMAIN=${DOMAIN:-atl73.ru}
EXTRA_NAMES=${EXTRA_NAMES:-www.$DOMAIN}
ACME_ROOT=${ACME_ROOT:-/var/www/acme}
CERT_LINK=${CERT_LINK:-/etc/raketa/cert}
HSTS_FILE=${HSTS_FILE:-/etc/raketa/hsts.conf}
# Пусто — регистрация без адреса: письма об истечении не придут, продлением
# занимается таймер certbot. Свой адрес сюда вписывает тот, кто хочет
# получать предупреждения (адрес уйдёт в Let's Encrypt — это внешняя служба).
ACME_EMAIL=${ACME_EMAIL:-}

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

live=/etc/letsencrypt/live/$DOMAIN
if [ -s "$live/fullchain.pem" ]; then
    say "сертификат уже выдан; продлением занимается certbot.timer"
    exit 0
fi

myip=$(curl -fsS -m 10 https://api.ipify.org || true)
if [ -z "$myip" ]; then
    say "не удалось узнать свой внешний адрес — пробую позже"
    exit 0
fi

names=()
for host in "$DOMAIN" $EXTRA_NAMES; do
    got=$(dig +short +time=5 +tries=2 "$host" A | tail -1)
    if [ "$got" = "$myip" ]; then
        names+=("-d" "$host")
    else
        say "$host → ${got:-ничего}, а нужно $myip — это имя пропускаю"
    fi
done

if [ ${#names[@]} -eq 0 ]; then
    say "домен пока указывает не на эту машину ($myip). Сертификат не запрашиваю:"
    say "проверка Let's Encrypt ушла бы на чужой адрес, а неудачные попытки"
    say "кончаются запретом на неделю. Поправьте A-запись — таймер подхватит сам."
    exit 0
fi

mkdir -p "$ACME_ROOT"
mail=(--register-unsafely-without-email)
[ -n "$ACME_EMAIL" ] && mail=(-m "$ACME_EMAIL")

say "запрашиваю сертификат: ${names[*]}"
certbot certonly --webroot -w "$ACME_ROOT" "${names[@]}" \
    --non-interactive --agree-tos "${mail[@]}" --keep-until-expiring

ln -sfn "$live" "$CERT_LINK"
cat > "$HSTS_FILE" <<'CONF'
# Сертификат настоящий — можно требовать HTTPS. Полгода и без preload:
# preload откатывается месяцами, а решение это не техническое.
add_header Strict-Transport-Security "max-age=15768000" always;
CONF
nginx -t && systemctl reload nginx
say "сертификат установлен, nginx перезапущен, HSTS включён"

# Продление само по себе ничего не перезапускает: nginx держит старый файл
# открытым и отдавал бы просроченный сертификат до ближайшей перезагрузки.
install -d /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh <<'CONF'
#!/bin/sh
nginx -t && systemctl reload nginx
CONF
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh

# Своя задача больше не нужна: дальше работает таймер certbot.
systemctl disable --now raketa-cert.timer 2>/dev/null || true
say "ожидание домена больше не нужно — таймер raketa-cert выключен"
