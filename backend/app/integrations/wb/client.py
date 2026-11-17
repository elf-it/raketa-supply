"""Клиент Wildberries.

Два клиента за одним интерфейсом:

* :class:`FixtureWbClient` — читает локальные JSON-файлы, на нём идёт разработка
  и тесты, ключи не нужны;
* :class:`HttpWbClient` — обращение к statistics-api WB.

HTTP-клиент написан по публичной документации, но **не проверен на боевых ключах**:
кабинеты и токены заказчик ещё не передал (вопрос Q-3, задача E-3 в бэклоге).
Пока `WB_CLIENT=fixtures`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@dataclass(frozen=True)
class WbCard:
    """Карточка номенклатуры кабинета."""

    nm_id: str
    vendor_code: str
    title: str
    category: str | None = None
    barcode: str | None = None


@dataclass(frozen=True)
class WbSaleRow:
    """Дневная строка продаж/заказов."""

    nm_id: str
    day: date
    order_units: float = 0.0
    buyout_units: float = 0.0
    sales_units: float = 0.0


@dataclass(frozen=True)
class WbStockRow:
    """Остаток на дату."""

    nm_id: str
    day: date
    quantity: float = 0.0


class WbClient(Protocol):
    def fetch_cards(self, store_code: str) -> list[WbCard]: ...

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]: ...

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]: ...


def _parse_day(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


class FixtureWbClient:
    """Фикстурный клиент: `fixtures/<store_code>.json`."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or FIXTURES_DIR

    def _load(self, store_code: str) -> dict:
        path = self.directory / f"{store_code}.json"
        if not path.exists():
            return {"cards": [], "sales": [], "stocks": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def fetch_cards(self, store_code: str) -> list[WbCard]:
        return [WbCard(**row) for row in self._load(store_code).get("cards", [])]

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]:
        rows = []
        for row in self._load(store_code).get("sales", []):
            day = _parse_day(row["day"])
            if day < date_from:
                continue
            rows.append(
                WbSaleRow(
                    nm_id=str(row["nm_id"]),
                    day=day,
                    order_units=row.get("order_units", 0.0),
                    buyout_units=row.get("buyout_units", 0.0),
                    sales_units=row.get("sales_units", 0.0),
                )
            )
        return rows

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]:
        rows = []
        for row in self._load(store_code).get("stocks", []):
            day = _parse_day(row["day"])
            if day < date_from:
                continue
            rows.append(
                WbStockRow(nm_id=str(row["nm_id"]), day=day, quantity=row.get("quantity", 0.0))
            )
        return rows


class HttpWbClient:
    """Реальный клиент statistics-api WB. Не проверен на боевых ключах (E-3)."""

    def __init__(self, tokens: dict[str, str], base_url: str, timeout: float = 60.0) -> None:
        self.tokens = tokens
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _token(self, store_code: str) -> str:
        token = self.tokens.get(store_code)
        if not token:
            raise RuntimeError(
                f"нет токена для кабинета {store_code!r}. "
                'Задайте WB_TOKENS="<код магазина>=<токен>,..." в окружении'
            )
        return token

    def _get(self, store_code: str, path: str, params: dict) -> list[dict]:
        import httpx

        headers = {"Authorization": self._token(store_code)}
        with httpx.Client(timeout=self.timeout) as client:
            response = client.get(f"{self.base_url}{path}", params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, list) else []

    def fetch_cards(self, store_code: str) -> list[WbCard]:
        # Карточки живут в content-api, а не в statistics-api; подключается вместе
        # с реальными ключами. Пока справочник ведётся в системе вручную.
        raise NotImplementedError("загрузка карточек из content-api WB не подключена, задача E-3")

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]:
        raw = self._get(
            store_code,
            "/api/v1/supplier/sales",
            {"dateFrom": date_from.isoformat(), "flag": 0},
        )
        rows: list[WbSaleRow] = []
        for item in raw:
            nm_id = str(item.get("nmId", ""))
            day = _parse_day(item["date"])
            # saleID, начинающийся на S, — продажа; на R — возврат.
            sale_id = str(item.get("saleID", ""))
            sign = -1.0 if sale_id.startswith("R") else 1.0
            rows.append(
                WbSaleRow(
                    nm_id=nm_id,
                    day=day,
                    order_units=0.0,
                    buyout_units=sign,
                    sales_units=sign,
                )
            )
        return rows

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]:
        raw = self._get(store_code, "/api/v1/supplier/stocks", {"dateFrom": date_from.isoformat()})
        return [
            WbStockRow(
                nm_id=str(item.get("nmId", "")),
                day=_parse_day(item.get("lastChangeDate", date_from.isoformat())),
                quantity=float(item.get("quantity", 0) or 0),
            )
            for item in raw
        ]


def make_client(
    kind: str, tokens: dict[str, str], base_url: str, fixtures_dir: Path | None = None
) -> WbClient:
    """Клиент по настройке. ``fixtures_dir`` — где лежат выгрузки для ``fixtures``.

    Пусто — рядом с кодом. Отдельная папка нужна там, где сгенерированные
    выгрузки не должны лежать в репозитории: у сида есть такой же ключ,
    и без этой настройки загрузка искала бы их не там, где он их положил.
    """
    if kind == "http":
        return HttpWbClient(tokens=tokens, base_url=base_url)
    return FixtureWbClient(fixtures_dir)
