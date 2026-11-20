"""Обновление демонстрационных данных без потери учётных записей (задача A-12).

На сервере с демо-данными свежесть загрузки протухает сама: `SYNC_STALE_HOURS`
по умолчанию 36 часов, а обновлять данные нечем — ключей кабинетов WB нет
(задача E-3). Через полтора дня готовность становится `degraded`, и это
правда, а не поломка: порог для такой машины поднимать не стоит, иначе он
замолчит и тогда, когда данные протухнут по-настоящему. Правильный ответ —
перезалить демо-данные перед показом.

Беда в том, что `python -m app.seed` задуман для пустой базы и чистит
таблицу пользователей вместе с остальными. На живой машине это значит, что
после каждого обновления данных все выданные пароли перестают работать без
предупреждения, а своего администратора приходится заводить заново.

Поэтому учётные записи снимаются до сида и возвращаются после — вместе
с хешами паролей, ролями и отметкой смены пароля. Выданные доступы остаются
рабочими; сессии всё равно закрываются, потому что строки пользователей
создаются заново и номера у них другие. Войти придётся ещё раз, прежним
паролем.

Запуск на сервере (в контейнере, из каталога /opt/raketa):

    docker compose -f docker-compose.prod.yml run --rm \\
      -v /opt/raketa/deploy/refresh-demo.py:/tmp/refresh.py backend \\
      python -c 'import runpy; runpy.run_path("/tmp/refresh.py")'

`runpy`, а не `python /tmp/refresh.py`: во втором случае первым в `sys.path`
встаёт /tmp, и пакет приложения не находится.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User

#: Выгрузки WB кладутся во временный каталог: каталог образа только для
#: чтения, а нужны они лишь этому прогону, который сам их и читает.
FIXTURES = Path(os.environ.get("RAKETA_FIXTURES_DIR", "/tmp/fixtures"))


def main() -> int:
    with SessionLocal() as session:
        kept = [
            {
                "email": user.email,
                "full_name": user.full_name,
                "role": user.role,
                "password_hash": user.password_hash,
                "is_active": user.is_active,
                "password_changed_at": user.password_changed_at,
            }
            for user in session.scalars(select(User).order_by(User.id)).all()
        ]

    print(f"снято учётных записей: {len(kept)}")
    if not kept:
        # Пустая база — обычный случай для первой установки, но тогда
        # и сид запускают напрямую. Здесь это скорее признак, что скрипт
        # запущен не там, где думали.
        print("в базе нет ни одного пользователя — обновление отменено")
        return 1

    # Импорт после снятия: сид чистит базу сразу при вызове.
    from app import seed

    seed.main(fixtures_dir=FIXTURES)

    with SessionLocal() as session:
        for row in kept:
            user = session.scalar(select(User).where(User.email == row["email"]))
            if user is None:
                user = User(email=row["email"])
                session.add(user)
            user.full_name = row["full_name"]
            user.role = row["role"]
            user.password_hash = row["password_hash"]
            user.is_active = row["is_active"]
            user.password_changed_at = row["password_changed_at"]
        session.commit()

        print("учётные записи возвращены:")
        for user in session.scalars(select(User).order_by(User.id)).all():
            print(f"  {user.role.value:7} {user.email:24} активна={user.is_active}")
    return 0


# Вызов безусловный, без проверки ``__name__``: через ``runpy.run_path``
# имя модуля не ``__main__``, и обычная проверка просто не сработала бы.
raise SystemExit(main())
