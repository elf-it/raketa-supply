"""Набор для развёртывания на сервере — согласованность файлов (задача A-10).

Эти файлы не исполняются ни тестами, ни линтером, а разъехаться им легко:
путь к сертификату написан в двух местах (шаблон nginx и cert.sh), путь
к команде развёртывания — в трёх (install.sh, принудительная команда ключа,
сам deploy.sh). Переименование в одном месте тихо ломает либо TLS, либо
выкладку, и узнаётся это на сервере.

Проверяется здесь только то, что можно проверить без сервера: что имена
и пути в разных файлах совпадают и что решения, на которых держится
безопасность выкладки, в этих файлах действительно записаны.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SITE = DEPLOY / "nginx-site.conf"
INSTALL = DEPLOY / "install.sh"
CERT = DEPLOY / "cert.sh"
REDEPLOY = DEPLOY / "deploy.sh"
WATCH = DEPLOY / "watch.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
COMPOSE = ROOT / "docker-compose.prod.yml"
EXAMPLE = ROOT / ".env.prod.example"
RUNBOOK = ROOT / "docs" / "09-runbook.md"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def site() -> str:
    return text(SITE)


def test_the_external_nginx_adds_exactly_one_hop(site):
    """Прокси стало два, и приложению нужно сказать об этом честно.

    `TRUSTED_PROXY_HOPS` — это не настройка производительности: по ней
    читается адрес клиента для счётчика попыток входа (задача G-12).
    Соври в меньшую сторону — и адрес возьмётся из того, что приписал сам
    клиент, то есть счётчик обходится подстановкой чужого адреса.
    """
    assert len(re.findall(r"proxy_set_header\s+X-Forwarded-For", site)) == 1
    assert "TRUSTED_PROXY_HOPS=2" in text(RUNBOOK), "в инструкции должно стоять 2"


def test_acme_is_served_from_the_directory_the_certificate_script_writes(site):
    """Проверка владения доменом ходит по открытому HTTP, до сертификата.

    Каталог записан в трёх файлах; разойдись они — certbot положит файл
    туда, где nginx его не ищет, и выдача провалится без внятной причины.
    """
    assert "location /.well-known/acme-challenge/" in site
    for path, name in ((SITE, "шаблон nginx"), (CERT, "cert.sh"), (INSTALL, "install.sh")):
        assert "/var/www/acme" in text(path), f"{name}: каталог проверки разошёлся"


def test_the_certificate_path_is_a_link_the_script_can_repoint(site):
    """Самоподписанный меняется на настоящий переставлением ссылки.

    Иначе пришлось бы править конфиг nginx из скрипта — то есть генерировать
    конфиг в двух состояниях и надеяться, что оба верны.
    """
    assert "/etc/raketa/cert/fullchain.pem" in site
    assert "/etc/raketa/cert/privkey.pem" in site
    assert "ln -sfn" in text(CERT) and "/etc/raketa/cert" in text(CERT)
    assert "ln -sfn" in text(INSTALL), "первая ссылка — на самоподписанный"


def test_hsts_arrives_only_with_a_real_certificate(site):
    """HSTS запрещает браузеру «пройти всё равно».

    Поставь его вместе с самоподписанным — и сайт закрыт насовсем, включая
    того, кто его разворачивает. Поэтому заголовок пишет cert.sh, а install.sh
    создаёт файл пустым: отсутствующий файл nginx не запустит вовсе.
    """
    assert "include /etc/raketa/hsts.conf;" in site
    assert "Strict-Transport-Security" not in site, "заголовок не в шаблоне"
    assert "Strict-Transport-Security" in text(CERT)
    assert "/etc/raketa/hsts.conf" in text(INSTALL)


def test_the_certificate_script_refuses_to_ask_for_a_foreign_domain():
    """Пока A-запись смотрит не сюда, запрашивать сертификат нельзя.

    Проверка уйдёт на чужую машину и провалится, а несколько таких провалов
    подряд Let's Encrypt наказывает запретом на неделю. Поэтому неподходящая
    A-запись здесь не ошибка, а причина подождать до следующего срабатывания.
    """
    body = text(CERT)

    assert "api.ipify.org" in body, "нужно знать свой адрес, чтобы сравнить"
    assert "dig +short" in body
    assert re.search(r"exit 0", body), "неподходящая запись — не ошибка"
    assert "--keep-until-expiring" in body


def test_redeploy_runs_the_installed_copy_not_the_file_it_overwrites():
    """Скрипт посреди работы перезаписывает сам себя.

    bash читает файл по мере выполнения: `git reset --hard` под работающим
    скриптом — это выполнение половины старой версии и половины новой.
    Поэтому работает копия в /usr/local/bin, и путь к ней должен совпадать
    с тем, что стоит в принудительной команде ключа.
    """
    body = text(REDEPLOY)
    install = text(INSTALL)

    assert "/usr/local/bin/raketa-deploy" in body
    assert "/usr/local/bin/raketa-deploy" in install
    assert 'command="/usr/local/bin/raketa-deploy"' in install, "ключ умеет только это"
    assert "git reset --quiet --hard" in body, "сервер руками не правят"
    # raketa-cert systemd запускает от root. Обновляй его deploy.sh — и право
    # на push в репозиторий стало бы правом на root на машине.
    assert "/usr/local/bin/raketa-cert" not in body


def test_redeploy_rolls_back_when_the_site_does_not_answer():
    """Выкладка, оставившая сайт лежать, хуже невыложенной правки."""
    body = text(REDEPLOY)

    assert "wait_healthy" in body
    assert "/api/v1/health" in body and "/api/v1/health/ready" in body
    assert body.count("git reset") >= 2, "второй reset — это откат на прежний коммит"
    assert "flock -n 9" in body, "два `up --build` на двух ядрах — это OOM"


def test_the_watcher_needs_nothing_but_the_key_it_already_has():
    """Приватному репозиторию запрос к API GitHub требует токена (задача A-11).

    Токен — это ещё один секрет на машине и ещё один срок его действия.
    Указатель проверенного лежит в самом репозитории и читается тем же
    ключом, которым сервер и так забирает код, поэтому сторож не должен
    обращаться ни к API, ни к чему-то, что просит отдельный доступ.
    """
    # Решение объяснено в шапке скрипта, поэтому проверяются исполняемые
    # строки: иначе тест сверял бы комментарий.
    code = "\n".join(line for line in text(WATCH).splitlines() if not line.lstrip().startswith("#"))

    assert "git ls-remote" in code
    assert "api.github.com" not in code, "API вернул бы токен в список секретов"
    assert "curl" not in code and "TOKEN" not in code


def test_the_watcher_deploys_only_what_ci_marked_green():
    """Пока проверки идут, указатель стоит на прежнем коммите — и тик молчит.

    Метку ставит сам CI после обеих проверок; имя ветки написано и в рабочем
    процессе, и в сторожке, и разъехаться им нельзя — иначе сторож ждал бы
    отметки, которую никто не ставит.
    """
    code = text(WATCH)
    # Разбор, а не поиск строки: те же слова стоят в комментариях рядом,
    # и поиск нашёл бы объяснение решения вместо самого решения.
    green = yaml.safe_load(text(ROOT / ".github" / "workflows" / "ci.yml"))["jobs"]["green"]
    pushes = " ".join(str(step.get("run", "")) for step in green["steps"])

    assert "ci-green" in code
    assert "refs/heads/ci-green" in pushes, "метку ставит CI, и имя у неё одно"
    assert green["needs"] == ["backend", "frontend"], "метка после обеих проверок"
    assert green["permissions"] == {"contents": "write"}, "иначе push отметки не пройдёт"
    assert "refs/heads/main" in green["if"], "метка только с main"
    assert "/usr/local/bin/raketa-deploy" in code
    assert "raketa-watch.timer" in text(INSTALL), "без таймера скрипт никто не позовёт"


def test_the_watcher_says_when_it_cannot_see_the_mark():
    """Молчащий сторож при пропавшем доступе — это «работает, а правки не едут».

    После перевода репозитория в приватный это главный способ сломаться:
    ключ отозван или не настроен, `ls-remote` возвращает пустоту, и сторож
    обязан сказать об этом в журнал, а не тихо ничего не делать.
    """
    code = text(WATCH)

    assert "нет отметки проверенного" in code
    assert "GitHub недоступен или нет доступа" in code


def test_the_deployment_takes_the_marked_commit_not_the_tip_of_main():
    """На main может лежать правка, о которой уже известно, что она сломана."""
    body = text(REDEPLOY)

    assert "DEPLOY_BRANCH:-ci-green" in body
    # Знак `+` в refspec: указатель может сдвинуться назад (перезапуск CI
    # на старом коммите), и без него выборка была бы отвергнута.
    assert "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" in body


def test_github_actions_deployment_stays_opt_in():
    """Основной путь — опрос с сервера; Actions быстрее, но ценой ключа.

    Приватный ключ в секретах читает всякий, кто может менять рабочие
    процессы, поэтому выбор оставлен человеку. Без переменной задание
    пропускается, а не падает: красный крест на каждом push означал бы
    «что-то сломано», когда ничего не сломано.
    """
    flow = yaml.safe_load(text(WORKFLOW))

    assert "vars.DEPLOY_ENABLED == 'true'" in flow["jobs"]["deploy"]["if"]


def test_the_workflow_deploys_only_green_main():
    """Выложить красный main — уронить сайт правкой, о которой уже известно."""
    flow = yaml.safe_load(text(WORKFLOW))
    # `on` в YAML — это True: ключ так и читается.
    trigger = flow[True]["workflow_run"]

    assert trigger["workflows"] == ["CI"]
    assert trigger["branches"] == ["main"]
    assert "conclusion == 'success'" in flow["jobs"]["deploy"]["if"]
    assert flow["concurrency"]["cancel-in-progress"] is False, "сборку не прерывают"


def test_the_workflow_keeps_the_server_out_of_a_public_repository():
    """Репозиторий публичный: адрес, пользователь и ключи — только в секретах."""
    flow = text(WORKFLOW)
    # Комментарии объясняют и обратное решение тоже, поэтому проверяется
    # то, что исполняется.
    code = "\n".join(line for line in flow.splitlines() if not line.lstrip().startswith("#"))

    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", flow), "адрес сервера в открытом файле"
    for secret in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS"):
        assert f"secrets.{secret}" in code
    assert "StrictHostKeyChecking=yes" in code, "иначе выкладка пойдёт на любую машину"
    assert "StrictHostKeyChecking=no" not in code


def test_the_published_port_can_be_closed_from_the_internet():
    """Публикация портов в Docker идёт мимо ufw.

    `0.0.0.0:8080` открыт из интернета, что бы ни говорил фаервол. Когда
    впереди свой nginx с TLS, порт нужен только с самой машины — иначе рядом
    с HTTPS остаётся открытый HTTP-вход в то же приложение.
    """
    compose = yaml.safe_load(text(COMPOSE))
    ports = compose["services"]["web"]["ports"]

    assert ports == ["${WEB_BIND:-0.0.0.0}:${WEB_PORT:-8080}:80"]
    assert "WEB_BIND=" in text(EXAMPLE)
    assert "WEB_BIND=127.0.0.1" in text(RUNBOOK), "в инструкции для сервера — локально"
