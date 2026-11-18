#!/usr/bin/env bash
# Сам замечает правки в GitHub и перезапускает (задача A-10). Таймер systemd
# раз в две минуты; на сервере он же команда `raketa-watch`.
#
# Решения и почему так:
#
# * **Сервер спрашивает GitHub, а не GitHub стучится на сервер.** Второй
#   способ (рабочий процесс Actions ходит по ssh) быстрее на эти две минуты,
#   но требует приватного ключа в секретах репозитория — а секреты читает
#   всякий, кто может менять рабочие процессы. Здесь наружу не открыто
#   ничего и хранить нечего. Вариант с Actions оставлен включаемым:
#   .github/workflows/deploy.yml и переменная DEPLOY_ENABLED.
#
# * **Сначала `git ls-remote`, и только потом API.** Опрос API каждые две
#   минуты — это 30 запросов в час при пределе 60 для неавторизованных,
#   то есть предел делился бы со всем, что ещё спросит GitHub с этого
#   адреса. `ls-remote` не ограничен ничем, и в обычный тик (правок нет)
#   запрос к API не уходит вовсе.
#
# * **Перезапуск только по зелёному CI.** Выложить красный main — значит
#   уронить сайт правкой, о которой уже известно, что она сломана. Пока
#   проверки идут, тик просто ничего не делает: следующий через две минуты.
#
# * **В журнал пишется только смена состояния.** Иначе красный CI давал бы
#   по строке каждые две минуты, и журнал пришлось бы читать по диагонали.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/raketa}
REPO=${REPO:-elf-it/raketa-supply}
BRANCH=${DEPLOY_BRANCH:-main}
STATE=${STATE:-$APP_DIR/.watch-state}
DEPLOY=${DEPLOY:-/usr/local/bin/raketa-deploy}

cd "$APP_DIR"

note() {
    # Повтор того же состояния в журнал не идёт.
    local line="$1"
    if [ "$(cat "$STATE" 2>/dev/null || true)" != "$line" ]; then
        printf '%s\n' "$line"
        printf '%s' "$line" > "$STATE"
    fi
}

remote=$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1)
if [ -z "$remote" ]; then
    note "GitHub не ответил на ls-remote"
    exit 0
fi
if [ "$remote" = "$(git rev-parse HEAD)" ]; then
    note "правок нет: $(git rev-parse --short HEAD)"
    exit 0
fi

short=${remote:0:7}
checks=$(curl -fsS -m 20 -H 'Accept: application/vnd.github+json' \
    "https://api.github.com/repos/$REPO/commits/$remote/check-runs" 2>/dev/null || true)
if [ -z "$checks" ]; then
    note "$short: GitHub не отдал состояние проверок"
    exit 0
fi

verdict=$(printf '%s' "$checks" | python3 -c '
import json, sys

# Пропущенное и необязательное — не красное. Иначе в списке проверок коммита
# оказывается выключенное задание выкладки (conclusion "skipped"), и сторож
# ждёт зелёного вечно. Это нашлось на живом прогоне: тесты такого не видят.
OK = {"success", "skipped", "neutral"}

runs = json.load(sys.stdin).get("check_runs") or []
if not runs:
    # Проверки могли ещё не начаться — это не повод выкладывать.
    print("нет проверок")
elif any(run.get("status") != "completed" for run in runs):
    print("проверки идут")
else:
    bad = [run["name"] for run in runs if run.get("conclusion") not in OK]
    print("красный: " + ", ".join(bad) if bad else "зелёный")
')

case "$verdict" in
    "зелёный")
        note "$short: CI зелёный — развёртываю"
        exec "$DEPLOY"
        ;;
    *)
        note "$short: $verdict — жду"
        ;;
esac
