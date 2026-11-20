#!/usr/bin/env bash
# Резервная копия базы с проверкой восстановления (задача G-21).
# Таймер systemd раз в сутки; на сервере он же команда `raketa-backup`.
#
# Решения и почему так:
#
# * **Каждая копия тут же разворачивается в отдельную базу и сверяется.**
#   Копия, которую ни разу не разворачивали, — это предположение о копии.
#   Проверяется не наличие файла: сверяются ревизия схемы и наполнение
#   ключевых таблиц. Файл правильного размера, который не восстанавливается,
#   хуже отсутствия файла — он создаёт уверенность.
#
# * **Снимок делает сам контейнер базы.** `pg_dump` из образа той же версии,
#   что и сервер: снимок, сделанный клиентом другой версии, восстанавливается
#   не всегда, и узнают об этом в худший день.
#
# * **Формат custom, а не текстовый.** Он сжат и позволяет восстанавливать
#   выборочно; текстовый дамп на той же базе втрое больше и льётся только
#   целиком.
#
# * **Срок хранения — число, а не «пока не кончится диск».** Четырнадцать
#   суточных копий: двух недель хватает, чтобы заметить порчу данных,
#   которую никто не видел неделю, и это 14 файлов, а не 14 гигабайт.
#   Окно потери данных при суточном снимке — до суток; меньше нужно
#   журналирование WAL, а это уже не этот скрипт.
#
# * **Копии лежат на той же машине, и это не резервное копирование от всех
#   беззаконий.** Потеря диска или VPS забирает и базу, и копии. Куда их
#   увозить — решение с чужим адресом и чужими ключами, его принимает
#   владелец данных (вопрос Q-16). Место для этого оставлено: BACKUP_SYNC_CMD.
#
# * **Пульс только после удачной копии.** О собственном молчании скрипт
#   сообщить не может, поэтому сторож снаружи ждёт сигнала и шумит, когда
#   тот пропал (как в задаче G-18). Пусто — пульс не отправляется.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/raketa}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.prod.yml}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/raketa}
#: Сколько суточных копий держать.
KEEP=${KEEP:-14}
#: База для проверки восстановления. Создаётся и удаляется каждым прогоном.
CHECK_DB=${CHECK_DB:-raketa_restore_check}
#: Куда увозить копии. Пусто — никуда, и это записано как ограничение.
SYNC_CMD=${BACKUP_SYNC_CMD:-}
#: Адрес сторожа снаружи; пинг уходит только после удачного прогона.
PULSE=${BACKUP_HEARTBEAT_URL:-}
STATUS=${STATUS:-$BACKUP_DIR/last-status}
#: Ниже этого размера файл не может быть настоящим снимком даже пустой базы.
MIN_BYTES=${MIN_BYTES:-2048}

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

cd "$APP_DIR"

# Из .env берутся только имя базы и пользователь: остальное там — секреты,
# и тащить их в окружение этого процесса незачем.
value_of() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1; }
PGUSER=$(value_of POSTGRES_USER); PGUSER=${PGUSER:-raketa}
PGDB=$(value_of POSTGRES_DB); PGDB=${PGDB:-raketa}

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
psql_in() { compose exec -T db psql -U "$PGUSER" -d "$1" -tAc "$2"; }

#: Чем сверяется восстановленная копия с живой базой.
COUNTS="select (select count(*) from users)||' '||(select count(*) from week_facts)||' '||(select count(*) from store_products)"
REVISION="select version_num from alembic_version"

file=""
ok=false

finish() {
    # База для проверки удаляется всегда: иначе после первого же сбоя она
    # останется лежать и следующий прогон упрётся в неё.
    compose exec -T db psql -U "$PGUSER" -d postgres -q \
        -c "drop database if exists $CHECK_DB" >/dev/null 2>&1 || true
    if $ok; then
        return 0
    fi
    # Неудачную копию оставлять нельзя: она выглядит как копия.
    [ -n "$file" ] && [ -f "$file" ] && rm -f "$file"
    printf 'СБОЙ %s\n' "$(date '+%F %T')" > "$STATUS" 2>/dev/null || true
    say "КОПИЯ НЕ СДЕЛАНА — разбирайтесь немедленно, предыдущие копии стареют"
}
trap finish EXIT

mkdir -p "$BACKUP_DIR"
chmod 0700 "$BACKUP_DIR"
umask 077

exec 9>"$BACKUP_DIR/.lock"
if ! flock -n 9; then
    say "предыдущий прогон ещё идёт — этот пропущен"
    ok=true
    exit 0
fi

say "снимок базы $PGDB"
file="$BACKUP_DIR/raketa-$(date '+%Y%m%d-%H%M').dump"
compose exec -T db pg_dump -U "$PGUSER" -d "$PGDB" --format=custom > "$file"

size=$(stat -c %s "$file")
if [ "$size" -lt "$MIN_BYTES" ]; then
    say "файл $size Б — это не снимок"
    exit 1
fi
say "снимок готов: $(basename "$file"), $size Б"

say "проверяю восстановлением в отдельную базу"
compose exec -T db psql -U "$PGUSER" -d postgres -q \
    -c "drop database if exists $CHECK_DB" -c "create database $CHECK_DB" >/dev/null
# --no-owner: владелец в проверочной базе не важен, а отличие ролей
# превратило бы проверку в разбор прав.
compose exec -T db pg_restore -U "$PGUSER" -d "$CHECK_DB" --no-owner < "$file" >/dev/null

live_rev=$(psql_in "$PGDB" "$REVISION" | tr -d '\r')
back_rev=$(psql_in "$CHECK_DB" "$REVISION" | tr -d '\r')
if [ "$live_rev" != "$back_rev" ] || [ -z "$back_rev" ]; then
    say "ревизия схемы в копии '$back_rev' вместо '$live_rev'"
    exit 1
fi

live_counts=$(psql_in "$PGDB" "$COUNTS" | tr -d '\r')
back_counts=$(psql_in "$CHECK_DB" "$COUNTS" | tr -d '\r')
for n in $back_counts; do
    if [ "$n" -le 0 ]; then
        say "в восстановленной копии пустые таблицы: $back_counts"
        exit 1
    fi
done
say "восстановлено: схема $back_rev, строк (люди, факты, пары) $back_counts"
if [ "$live_counts" != "$back_counts" ]; then
    # Не повод считать копию негодной: между снимком и сверкой кто-то мог
    # записать. Но числа должны быть видны — расхождение в разы это уже не
    # «кто-то записал».
    say "в живой базе сейчас $live_counts — расхождение со снимком возможно при записи"
fi

removed=$(ls -1t "$BACKUP_DIR"/raketa-*.dump 2>/dev/null | tail -n "+$((KEEP + 1))" || true)
if [ -n "$removed" ]; then
    printf '%s\n' "$removed" | xargs -r rm -f
    say "старых копий удалено: $(printf '%s\n' "$removed" | wc -l), хранится $KEEP"
fi

if [ -n "$SYNC_CMD" ]; then
    say "увожу копию наружу"
    BACKUP_FILE="$file" sh -c "$SYNC_CMD"
else
    say "наружу копии не увозятся: BACKUP_SYNC_CMD не задан — потеря машины забирает и базу, и копии"
fi

printf 'ok %s %s %s\n' "$(date '+%F %T')" "$(basename "$file")" "$back_counts" > "$STATUS"
ok=true

if [ -n "$PULSE" ]; then
    curl -fsS -m 10 -o /dev/null "$PULSE" \
        && say "пульс отправлен сторожу" \
        || say "пульс не ушёл — сторож поднимет тревогу, и это правильно"
fi

say "готово: $(basename "$file")"
