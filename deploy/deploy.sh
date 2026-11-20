#!/usr/bin/env bash
# Перезапуск после правок (задача A-10). Вызывается тремя способами:
#
#   GitHub Actions → ssh deploy@сервер   (принудительная команда, см. install.sh)
#   руками:          raketa-deploy
#   руками, без ssh: bash /opt/raketa/deploy/deploy.sh
#
# Решения и почему так:
#
# * **Скрипт работает с копии самого себя.** bash читает файл по мере
#   выполнения, а этот скрипт посреди работы делает `git reset --hard`, то
#   есть перезаписывает собственный файл: дальше читалась бы середина новой
#   версии, и прогон падал бы синтаксической ошибкой. Поэтому первым делом
#   исполняемый текст копируется в /tmp и работа продолжается с копии.
#   Так же сделано в watch.sh и backup.sh — их тоже перезаписывает выкладка.
#
#   Прежде вместо этого в /usr/local/bin лежали копии, которые прогон
#   обновлял в конце. Обе половины той схемы оказались сломаны: `install`
#   не мог заменить файл (нужно право записи в каталог, а не на файл),
#   а когда замену починили — она перезаписала работающий скрипт и прогон
#   упал уже после удачной выкладки. Копия в /tmp снимает и то, и другое:
#   обновлять нечего, в /usr/local/bin лежат ссылки на файлы репозитория,
#   и правка вступает в силу сразу, а не «со следующего раза».
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

# Работа с копии — см. первое решение в шапке. Признак «уже с копии» — путь
# самого файла, а не переменная окружения: переменную унаследовал бы exec
# в дочерний скрипт, и тот удалил бы по `$0` не копию, а установленную ссылку.
case "$0" in
    /tmp/raketa-run.*) trap 'rm -f "$0"' EXIT ;;
    *)
        self=$(mktemp /tmp/raketa-run.XXXXXX)
        cat "$0" > "$self"
        exec bash "$self" "$@"
        ;;
esac

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

# Обновлять установленные копии больше не нужно: в /usr/local/bin лежат
# ссылки на файлы репозитория, а исполняется копия в /tmp, сделанная в начале
# прогона. Правка скрипта вступает в силу сразу со следующего запуска.

say "готово: $(git rev-parse --short HEAD)"
