#!/usr/bin/env bash
# Перезапуск после правок (задача A-10). Вызывается тремя способами:
#
#   GitHub Actions → ssh deploy@сервер   (принудительная команда, см. install.sh)
#   руками:          raketa-deploy
#   руками, без ssh: bash /opt/raketa/deploy/deploy.sh
#
# Решения и почему так:
#
# * **Работает установленная копия, а не файл из репозитория.** bash читает
#   скрипт по мере выполнения, а этот скрипт посреди работы делает
#   `git reset --hard` — то есть перезаписывает сам себя. Поэтому install.sh
#   кладёт копию в /usr/local/bin/raketa-deploy, принудительная команда ssh
#   запускает её, и в конце прогона копия обновляется: правка deploy.sh
#   вступает в силу со следующего развёртывания. Это заметно в журнале.
#
# * **Сервер не правят руками: `git reset --hard`, а не `git pull`.** Слияние
#   на боевой машине кончается конфликтом в три часа ночи. Настройки не
#   страдают: .env в .gitignore, его reset не трогает.
#
# * **Выкладывается проверенное, а не вершина main.** Указатель `ci-green`
#   ставит сам CI (задача A-11): на main может лежать правка, о которой уже
#   известно, что она сломана.
#
# * **Откат при неудаче.** Если после сборки приложение не отвечает, скрипт
#   возвращает прежний коммит и поднимает его заново. Выкладка, оставившая
#   сайт лежать, хуже невыложенной правки.
#
# * **Один развёртыватель за раз.** Два `up --build` на двух ядрах не быстрее,
#   а OOM.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/raketa}
#: Выкладывается `ci-green` — указатель на коммит, прошедший проверки
#: (задача A-11), а не `main`: на main может лежать правка, о которой уже
#: известно, что она сломана. Выложить что-то другое: DEPLOY_BRANCH=main.
BRANCH=${DEPLOY_BRANCH:-ci-green}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.prod.yml}
# Проверка живости идёт через тот же nginx, что отдаёт сайт: так проверяется
# вся цепочка, а не один контейнер.
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:8080/api/v1/health}
READY_URL=${READY_URL:-http://127.0.0.1:8080/api/v1/health/ready}
HEALTH_TRIES=${HEALTH_TRIES:-40}
INSTALLED_COPY=${INSTALLED_COPY:-/usr/local/bin/raketa-deploy}
INSTALLED_WATCH=${INSTALLED_WATCH:-/usr/local/bin/raketa-watch}
INSTALLED_BACKUP=${INSTALLED_BACKUP:-/usr/local/bin/raketa-backup}

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

cd "$APP_DIR"

exec 9>"$APP_DIR/.deploy.lock"
if ! flock -n 9; then
    say "развёртывание уже идёт — этот запуск пропущен"
    exit 0
fi

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

wait_healthy() {
    local i
    for i in $(seq 1 "$HEALTH_TRIES"); do
        if curl -fsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
            say "приложение отвечает (попытка $i)"
            return 0
        fi
        sleep 3
    done
    return 1
}

before=$(git rev-parse HEAD)
say "было: $(git log --oneline -1)"

# Refspec целиком и со знаком `+`: указатель проверенного может сдвинуться
# назад (перезапуск CI на старом коммите), и без принудительного обновления
# такая выборка была бы отвергнута как не-fast-forward.
git fetch --quiet origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
git reset --quiet --hard "refs/remotes/origin/$BRANCH"
after=$(git rev-parse HEAD)

if [ "$before" = "$after" ]; then
    say "новых правок нет, но пересборку делаю: запуск мог быть ради неё"
else
    say "стало: $(git log --oneline -1)"
fi

say "сборка и запуск"
compose up -d --build --remove-orphans

if wait_healthy; then
    say "готовность: $(curl -fsS -m 10 "$READY_URL" 2>/dev/null || echo 'не прочитана')"
else
    say "ПРИЛОЖЕНИЕ НЕ ОТВЕТИЛО после сборки — откатываюсь на $before"
    compose logs --tail 40 backend || true
    if [ "$before" != "$after" ]; then
        git reset --quiet --hard "$before"
        compose up -d --build --remove-orphans
        if wait_healthy; then
            say "откат удался: на сервере снова $(git log --oneline -1)"
        else
            say "откат НЕ помог — сайт лежит, нужен человек"
        fi
    fi
    exit 1
fi

# Старые слои съедают диск за несколько десятков выкладок. Тома не трогаем:
# в них база.
docker image prune -f >/dev/null 2>&1 || true

# Обновление установленных копий — последним делом, когда прогон уже удался.
# Обновляются только те, что работают от того же пользователя: raketa-cert
# systemd запускает от root, и позволять правкам в репозитории менять его
# содержимое значило бы делать из права на push право на root. Его обновляет
# install.sh, то есть человек с правами root.
#
# Запись идёт **в существующий файл**, а не заменой файла. `install` и `cp`
# сначала снимают прежний, а для этого нужно право записи в каталог, которого
# у `deploy` на /usr/local/bin нет и быть не должно: владеть файлом мало.
# Именно на этом обновление копий молча не работало, а `set -e` ещё и ронял
# прогон уже после удачной выкладки — то есть удавшееся развёртывание
# сообщало об ошибке. Права и владелец копии при такой записи сохраняются.
update_copy() {
    local src=$1 dst=$2
    cmp -s "$src" "$dst" && return 0
    if cat "$src" > "$dst" 2>/dev/null; then
        say "$(basename "$dst") обновлён — новая версия сработает со следующего раза"
    else
        # Не повод считать выкладку неудачной: приложение уже работает.
        # Но и молчать нельзя — правка скрипта иначе не вступит в силу никогда.
        say "НЕ УДАЛОСЬ обновить $dst — правка скрипта не вступит в силу, нужен root"
    fi
}

update_copy "$APP_DIR/deploy/deploy.sh" "$INSTALLED_COPY"
update_copy "$APP_DIR/deploy/watch.sh" "$INSTALLED_WATCH"
update_copy "$APP_DIR/deploy/backup.sh" "$INSTALLED_BACKUP"

say "готово: $(git rev-parse --short HEAD)"
