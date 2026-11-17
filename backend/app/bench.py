"""Нагрузочный прогон по работающей системе (задача G-6).

    python -m app.bench --users 5 --seconds 20

Замеры в `docs/08-performance.md` до этого были одиночными: один запрос, один
процесс, никого рядом. Так меряется расчёт, но не система — а вопрос у заказчика
другой: «выдержит ли она нас». Здесь он и меряется: несколько человек
одновременно ходят по экранам, как ходят люди.

Решения и почему так:

* **Прогон ходит по HTTP, а не зовёт функции.** В одиночных замерах через
  `TestClient` не видно ни сериализации в сеть, ни конкуренции за соединения
  к базе, ни того, что воркер один. Именно это и упирается первым.

* **У каждого «человека» своя сессия.** Отдельный клиент с отдельной кукой:
  общий клиент на все потоки мерил бы один пул соединений и одну куку,
  а это не то, что происходит в жизни.

* **Холодный и тёплый запрос считаются порознь.** Первый запрос после
  перезапуска платит за весь кэш расчёта (задача G-13), и смешивать его
  с остальными значит прятать и то, и другое.

* **Правки по умолчанию выключены.** Прогон можно запустить по боевой системе,
  чтобы померить на настоящих объёмах (вопрос Q-9) — и он не должен при этом
  ничего менять. Ключ ``--write`` включает правку ячейки: она нужна, чтобы
  увидеть цену сброса кэша, но включается осознанно.

* **Пароль — через окружение.** ``RAKETA_BENCH_PASSWORD``; по умолчанию
  демонстрационный редактор из сида. Пароль в командной строке попадает
  в историю оболочки и в список процессов — так же, как в команде
  первого администратора, здесь этого нет.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

#: Демонстрационный редактор из сида: его учётные данные и так в репозитории.
DEFAULT_EMAIL = "editor@raketa.local"
DEFAULT_PASSWORD = "demo-editor-2026"
PASSWORD_ENV = "RAKETA_BENCH_PASSWORD"

DEFAULT_BASE = "http://127.0.0.1:8000"
API = "/api/v1"


@dataclass
class Samples:
    """Замеры одного шага сценария."""

    name: str
    first_ms: float | None = None
    first_bytes: int = 0
    times: list[float] = field(default_factory=list)
    bytes: list[int] = field(default_factory=list)
    errors: int = 0

    def add(self, ms: float, size: int, cold: bool) -> None:
        if cold and self.first_ms is None:
            self.first_ms, self.first_bytes = ms, size
            return
        self.times.append(ms)
        self.bytes.append(size)

    def quantile(self, share: float) -> float:
        if not self.times:
            return 0.0
        ordered = sorted(self.times)
        index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
        return ordered[index]

    @property
    def median_bytes(self) -> int:
        return int(statistics.median(self.bytes)) if self.bytes else 0


@dataclass
class Step:
    """Шаг сценария: что человек делает на экране."""

    name: str
    path: str
    params: dict | None = None
    method: str = "GET"
    json: dict | None = None


def screen_steps(limit: int) -> list[Step]:
    """Сценарий обычной работы: открыть, пролистать, посмотреть иначе, закупка."""
    return [
        Step("матрица, первая страница", f"{API}/matrix", {"level": "sku", "limit": limit}),
        Step(
            "матрица, следующая страница",
            f"{API}/matrix",
            {"level": "sku", "limit": limit, "offset": limit},
        ),
        Step("матрица по магазинам", f"{API}/matrix", {"level": "store"}),
        Step(
            "матрица с отбором по статусу",
            f"{API}/matrix",
            {"level": "sku", "limit": limit, "status": "SHORT"},
        ),
        # Закупка берёт такое же окно строк, как матрица (задача G-17):
        # сценарий должен повторять экран, а не ручку без окна.
        Step("закупка", f"{API}/purchase", {"only_needed": "false", "limit": limit}),
        Step("плашка свежести данных", f"{API}/sync/status"),
    ]


class Person:
    """Один человек со своей сессией."""

    def __init__(self, base: str, email: str, password: str, timeout: float) -> None:
        self.client = httpx.Client(base_url=base.rstrip("/"), timeout=timeout)
        answer = self.client.post(f"{API}/auth/login", json={"email": email, "password": password})
        if answer.status_code != 200:
            raise RuntimeError(
                f"вход не удался ({answer.status_code}). Проверьте адрес, почту "
                f"и переменную {PASSWORD_ENV}"
            )

    def do(self, step: Step) -> tuple[float, int, bool]:
        started = time.perf_counter()
        if step.method == "PUT":
            answer = self.client.put(step.path, json=step.json or {})
        else:
            answer = self.client.get(step.path, params=step.params)
        spent = (time.perf_counter() - started) * 1000
        return spent, len(answer.content), answer.status_code < 400

    def close(self) -> None:
        self.client.close()


def _pairs_and_week(person: Person, limit: int) -> tuple[list[int], str]:
    """Что править: несколько позиций и первая незакрытая неделя."""
    answer = person.client.get(f"{API}/matrix", params={"level": "sku", "limit": limit})
    answer.raise_for_status()
    body = answer.json()
    weeks = [week["key"] for week in body["weeks"] if not week["is_closed"]]
    pairs = [row["meta"]["pair_id"] for row in body["rows"] if row["meta"].get("pair_id")]
    return pairs, (weeks[0] if weeks else "")


def run(
    base: str,
    users: int,
    seconds: float,
    limit: int,
    email: str,
    password: str,
    write: bool,
    timeout: float,
) -> dict[str, Samples]:
    """Гоняет сценарий ``users`` потоками ``seconds`` секунд."""
    people = [Person(base, email, password, timeout) for _ in range(users)]
    steps = screen_steps(limit)

    if write:
        pairs, week = _pairs_and_week(people[0], limit)
        if pairs and week:
            # Правка одной ячейки: после неё пара пересчитывается заново,
            # и видно, чего стоит сброс кэша (задача G-13).
            steps.append(
                Step(
                    "правка ячейки",
                    f"{API}/forecast/{pairs[0]}/{week}",
                    method="PUT",
                    json={"diff_percent": 110},
                )
            )
            steps.append(Step("строка после правки", f"{API}/matrix/row", {"pair_id": pairs[0]}))

    results: dict[str, Samples] = {step.name: Samples(step.name) for step in steps}
    lock_free: dict[str, list] = defaultdict(list)
    deadline = time.perf_counter() + seconds

    def worker(person: Person, cold: bool) -> list[tuple[str, float, int, bool, bool]]:
        out: list[tuple[str, float, int, bool, bool]] = []
        first = cold
        while time.perf_counter() < deadline:
            for step in steps:
                spent, size, ok = person.do(step)
                out.append((step.name, spent, size, ok, first))
            first = False
        return out

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=users) as pool:
        # Холодным считается только первый проход первого человека: кэш общий
        # на процесс, и остальные уже придут на тёплое.
        futures = [pool.submit(worker, person, index == 0) for index, person in enumerate(people)]
        for future in futures:
            for name, spent, size, ok, cold in future.result():
                lock_free[name].append((spent, size, ok, cold))
    spent_total = time.perf_counter() - started

    for person in people:
        person.close()

    for name, rows in lock_free.items():
        samples = results[name]
        for spent, size, ok, cold in rows:
            if not ok:
                samples.errors += 1
                continue
            samples.add(spent, size, cold)
    results["__total__"] = Samples("__total__")
    results["__total__"].times = [spent_total]
    return results


def report(results: dict[str, Samples], users: int, seconds: float) -> int:
    """Печатает таблицу. Возвращает код возврата: 1 — были ошибки."""
    total = results.pop("__total__", None)
    elapsed = total.times[0] if total and total.times else seconds
    requests = sum(len(s.times) + (1 if s.first_ms is not None else 0) for s in results.values())
    errors = sum(s.errors for s in results.values())

    print(
        f"\nЛюдей одновременно: {users}, время прогона: {elapsed:.1f} с, "
        f"запросов: {requests} ({requests / elapsed:.1f} в секунду)"
    )
    if errors:
        print(f"ОШИБОК: {errors}", file=sys.stderr)

    head = (
        f"{'шаг':<34}{'N':>5}{'холодный':>11}{'медиана':>10}"
        f"{'p95':>10}{'макс':>10}{'ответ':>11}{'ошибок':>8}"
    )
    print(head)
    print("-" * len(head))
    for samples in results.values():
        cold = f"{samples.first_ms:.0f} мс" if samples.first_ms is not None else "—"
        size = samples.median_bytes or samples.first_bytes
        print(
            f"{samples.name:<34}{len(samples.times):>5}{cold:>11}"
            f"{samples.quantile(0.5):>7.0f} мс"
            f"{samples.quantile(0.95):>7.0f} мс"
            f"{max(samples.times, default=0):>7.0f} мс"
            f"{size / 1024:>8.0f} КБ"
            f"{samples.errors:>8}"
        )
    return 1 if errors else 0


def main(
    base: str = DEFAULT_BASE,
    users: int = 5,
    seconds: float = 20.0,
    limit: int = 200,
    email: str = DEFAULT_EMAIL,
    write: bool = False,
    timeout: float = 120.0,
) -> int:
    password = os.environ.get(PASSWORD_ENV) or DEFAULT_PASSWORD
    print(f"Прогон по {base} от {email}")
    try:
        results = run(base, users, seconds, limit, email, password, write, timeout)
    except (httpx.HTTPError, RuntimeError) as error:
        print(f"Прогон не состоялся: {error}", file=sys.stderr)
        return 1
    return report(results, users, seconds)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Нагрузочный прогон по работающей системе",
        epilog=(
            "Пароль берётся из переменной окружения "
            f"{PASSWORD_ENV} (по умолчанию — демонстрационный редактор). "
            "Коды возврата: 0 — прогон прошёл, 1 — были ошибки."
        ),
    )
    parser.add_argument("--base", default=DEFAULT_BASE, help="адрес системы")
    parser.add_argument("--users", type=int, default=5, help="сколько человек одновременно")
    parser.add_argument("--seconds", type=float, default=20.0, help="сколько секунд гонять")
    parser.add_argument("--limit", type=int, default=200, help="строк на странице матрицы")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="кем входить")
    parser.add_argument(
        "--write",
        action="store_true",
        help="включить правку ячейки: прогон начнёт менять данные",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="таймаут запроса в секундах")
    args = parser.parse_args()
    raise SystemExit(
        main(
            base=args.base,
            users=args.users,
            seconds=args.seconds,
            limit=args.limit,
            email=args.email,
            write=args.write,
            timeout=args.timeout,
        )
    )
