# Развёртывание на сервере

Что здесь лежит:

| Файл | Зачем |
| --- | --- |
| `install.sh` | разовая настройка машины: nginx, сертификат, команды, таймер, ключ для Actions |
| `nginx-site.conf` | шаблон сайта для внешнего nginx (TLS и домен) |
| `deploy.sh` | перезапуск после правок; на сервере он же `raketa-deploy` |
| `watch.sh` | замечает метку `ci-green` и перезапускает по ней; он же `raketa-watch` |
| `cert.sh` | настоящий сертификат, как только домен укажет на машину; он же `raketa-cert` |
| `refresh-demo.py` | перезалить демо-данные, не потеряв учётные записи и пароли |

Порядок, журнал команд, что делать при сбое и чего это решение не умеет —
в [docs/09-runbook.md](../docs/09-runbook.md), раздел «Сервер atl73.ru».

Коротко, с нуля (репозиторий приватный — нужен ключ развёртывания
на чтение, порядок в рунбуке):

```sh
git clone git@github.com:elf-it/raketa-supply.git /opt/raketa
DOMAIN=atl73.ru SERVER_IP=<адрес> bash /opt/raketa/deploy/install.sh /tmp/actions.pub
cp /opt/raketa/.env.prod.example /opt/raketa/.env   # заполнить
raketa-deploy
```

`.env` не хранится в репозитории и `git reset --hard` его не трогает: в нём
пароль базы и ключ подписи сессий.
