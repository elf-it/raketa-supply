"""Ограничение частоты попыток входа.

Без него подбор пароля замедляет только стоимость bcrypt — это сотни попыток
в минуту с одной машины. Счётчик неудачных попыток закрывает вход на окно
времени и делает перебор бессмысленно медленным.

Решения и почему так:

* **Ключ — пара (почта, адрес), а не почта.** Счётчик по одной почте
  превращается в способ заблокировать коллегу: достаточно слать за него
  неверные пароли. С адресом в ключе чужие попытки не мешают человеку войти
  со своего места.
* **Отдельный счётчик по адресу** — на случай перебора не пароля, а почты:
  по одной попытке на сотню учётных записей не набрала бы лимит ни одна пара.
  Порог у него намеренно выше: за одним адресом может сидеть целый офис.
* **Удачный вход очищает счётчик пары,** иначе человек, вспомнивший пароль
  с пятой попытки, остался бы заперт на четверть часа.
* **Счётчик живёт в памяти процесса.** Этого достаточно для одного процесса
  приложения. За несколькими воркерами лимит делится на их число, а при
  перезапуске обнуляется: честная защита от перебора в проде — общий счётчик
  (Redis) или ограничение на уровне прокси. Здесь важнее, чтобы перебор
  перестал быть бесплатным, чем чтобы счётчик был точным.
* **Адрес берётся из соединения.** За обратным прокси без проброса заголовков
  все клиенты выглядят одним адресом, и работает только счётчик по адресу —
  поэтому его порог и задаётся настройкой: в такой схеме его поднимают.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from functools import lru_cache

from .config import get_settings


class LoginGuard:
    """Скользящее окно неудачных попыток входа."""

    def __init__(self, per_account: int, per_address: int, window_seconds: int) -> None:
        self.per_account = per_account
        self.per_address = per_address
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        # Синхронные обработчики FastAPI живут в пуле потоков: словарь общий.
        self._lock = threading.Lock()

    @staticmethod
    def _account_key(email: str, address: str) -> str:
        return f"account:{email.strip().lower()}|{address}"

    @staticmethod
    def _address_key(address: str) -> str:
        return f"address:{address}"

    def _retry_after(self, key: str, limit: int, now: float) -> int:
        """Сколько секунд ждать по этому ключу; 0 — можно пробовать."""
        hits = self._hits.get(key)
        if hits is None:
            return 0
        fresh = [moment for moment in hits if now - moment < self.window_seconds]
        if fresh:
            self._hits[key] = fresh
        else:
            self._hits.pop(key, None)
        if len(fresh) < limit:
            return 0
        # Ждать нужно до того момента, когда из окна выпадет самая старая попытка.
        return max(1, int(self.window_seconds - (now - fresh[0]) + 1))

    def retry_after(self, email: str, address: str) -> int:
        """Сколько секунд осталось до следующей разрешённой попытки."""
        now = time.monotonic()
        with self._lock:
            return max(
                self._retry_after(self._account_key(email, address), self.per_account, now),
                self._retry_after(self._address_key(address), self.per_address, now),
            )

    def failure(self, email: str, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            self._hits[self._account_key(email, address)].append(now)
            self._hits[self._address_key(address)].append(now)

    def success(self, email: str, address: str) -> None:
        """Удачный вход снимает счётчик пары, но не общий счётчик адреса."""
        with self._lock:
            self._hits.pop(self._account_key(email, address), None)

    def reset(self) -> None:
        """Полная очистка — нужна тестам, чтобы они не мешали друг другу."""
        with self._lock:
            self._hits.clear()


@lru_cache
def get_login_guard() -> LoginGuard:
    settings = get_settings()
    return LoginGuard(
        per_account=settings.login_max_attempts,
        per_address=settings.login_max_attempts_per_address,
        window_seconds=settings.login_window_minutes * 60,
    )
