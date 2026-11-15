"""Загрузка номенклатуры файлом (задача B-15).

Ведение справочников руками (B-14) закрывает правку и заведение по одному,
но первый каталог так не наполнить: у заказчика сотни артикулов в нескольких
кабинетах. Этот модуль читает книгу Excel и заводит из неё товары, категории,
поставщиков и пары (магазин, товар).

Решения и почему так:

* **Одна строка — товар в кабинете.** Номер номенклатуры (`nmId`) живёт на паре,
  а не на товаре: один артикул в трёх кабинетах — это три номера. Поэтому
  повторяющийся артикул в файле не ошибка, а тот же товар в другом кабинете;
  карточка заводится один раз, пары — по строке на каждую.

* **Колонки ищутся по названию, а не по месту.** Файл человек соберёт из своей
  выгрузки, и порядок колонок там будет свой. Лишние колонки просто не читаются,
  обязательная одна — «Артикул».

* **Пустая ячейка ничего не стирает.** В файле, собранном из чужой выгрузки,
  половина колонок будет пустой, и принять пустоту за «обнулить» значит снести
  параметры закупки у всего каталога. Пустое — «не трогать».

* **Сначала разбор, потом запись.** Файл приходит дважды: первый раз система
  показывает, что именно изменится, второй — применяет. Между ними она ничего
  не помнит, поэтому второй разбор считается заново.

* **Пишет не этот модуль.** Товары, пары и справочники заводятся через
  ``services/catalog.py``, цены — через ``services/prices.py``. Иначе загрузка
  обошла бы правила, ради которых те сервисы существуют: единственного писаря
  в цену, уникальность номера номенклатуры внутри кабинета, запрет удаления
  того, на что ссылаются. Цена из файла становится записью истории с пометкой
  «загрузка из файла», а не числом без прошлого.

* **Кабинеты файл не заводит.** За кабинетом стоит ключ API в окружении, и завести
  его между делом строкой таблицы нельзя: строка с незнакомым кабинетом
  отклоняется с объяснением. Категории и поставщики — наоборот, заводятся:
  их в файле десятки, и предпросмотр показывает список новых до записи.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from io import BytesIO

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Category, Product, Store, StoreProduct, Supplier
from . import catalog as catalog_service
from . import prices as price_service

#: Пометка в истории цен: по ней видно, откуда взялась цена.
PRICE_NOTE = "загрузка из файла"

#: Потолки. Файл на пять тысяч строк — это каталог крупнее любого ожидаемого
#: (вопрос Q-9), а десять мегабайт книги при таком числе строк недостижимы.
MAX_ROWS = 5000
MAX_FILE_BYTES = 10 * 1024 * 1024

#: Колонки файла: название в шапке → поле строки. Регистр и лишние пробелы
#: не важны, порядок — тоже.
COLUMNS: dict[str, str] = {
    "артикул": "vendor_code",
    "название": "title",
    "товар": "title",
    "категория": "category",
    "ниша": "niche",
    "поставщик": "supplier",
    "себестоимость": "cost_price",
    "цена": "price",
    "объём, м³": "volume_m3",
    "объем, м3": "volume_m3",
    "срок поставки, дней": "lead_time_days",
    "moq": "moq",
    "кратность": "pack_size",
    "покрытие, недель": "coverage_weeks",
    "страховой запас, недель": "safety_weeks",
    "кабинет": "store",
    "номенклатура wb": "nm_id",
    "nmid": "nm_id",
    "штрихкод": "barcode",
    "базовый потенциал": "base_potential",
}

#: Что обязательно должно быть в шапке, чтобы файл считался файлом номенклатуры.
REQUIRED_COLUMN = "артикул"

#: Колонки в том виде, в каком их пишет выгрузка каталога (она же шаблон).
#: Загрузка ищет колонки по названию, так что порядок здесь — только читаемость;
#: а вот совпадение названий с ``COLUMNS`` обязательно, иначе система не прочитала
#: бы собственную выгрузку. За этим следит тест.
EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("Артикул", "vendor_code"),
    ("Название", "title"),
    ("Категория", "category"),
    ("Ниша", "niche"),
    ("Поставщик", "supplier"),
    ("Себестоимость", "cost_price"),
    ("Цена", "price"),
    ("Объём, м³", "volume_m3"),
    ("Срок поставки, дней", "lead_time_days"),
    ("MOQ", "moq"),
    ("Кратность", "pack_size"),
    ("Покрытие, недель", "coverage_weeks"),
    ("Страховой запас, недель", "safety_weeks"),
    ("Кабинет", "store"),
    ("Номенклатура WB", "nm_id"),
    ("Штрихкод", "barcode"),
    ("Базовый потенциал", "base_potential"),
]

TEXT_FIELDS = ("vendor_code", "title", "category", "niche", "supplier", "store", "nm_id", "barcode")
INT_FIELDS = ("lead_time_days", "moq", "pack_size", "coverage_weeks")
FLOAT_FIELDS = ("cost_price", "price", "volume_m3", "safety_weeks", "base_potential")

#: Поля карточки товара, которые файл может изменить, и их подписи для предпросмотра.
PRODUCT_FIELDS: dict[str, str] = {
    "title": "название",
    "volume_m3": "объём",
    "lead_time_days": "срок поставки",
    "moq": "MOQ",
    "pack_size": "кратность",
    "coverage_weeks": "покрытие",
    "safety_weeks": "страховой запас",
}


class CatalogImportError(ValueError):
    """Файл не годится целиком: не книга, нет шапки, слишком велик."""


@dataclass
class Tally:
    """Счётчик с примерами: по одному числу не понять, что именно произойдёт."""

    count: int = 0
    examples: list[str] = field(default_factory=list)

    def add(self, example: str) -> None:
        self.count += 1
        if len(self.examples) < 20:
            self.examples.append(example)


@dataclass
class Plan:
    """Что файл сделает (или сделал) с каталогом."""

    rows_read: int = 0
    applied: bool = False
    new_categories: Tally = field(default_factory=Tally)
    new_suppliers: Tally = field(default_factory=Tally)
    new_products: Tally = field(default_factory=Tally)
    updated_products: Tally = field(default_factory=Tally)
    new_pairs: Tally = field(default_factory=Tally)
    updated_pairs: Tally = field(default_factory=Tally)
    price_records: Tally = field(default_factory=Tally)
    #: Строки, которые в работу не взяли, — с причиной и номером строки файла.
    issues: Tally = field(default_factory=Tally)

    @property
    def total_changes(self) -> int:
        return (
            self.new_categories.count
            + self.new_suppliers.count
            + self.new_products.count
            + self.updated_products.count
            + self.new_pairs.count
            + self.updated_pairs.count
            + self.price_records.count
        )


@dataclass(frozen=True)
class FileRow:
    """Строка файла: номер строки нужен, чтобы человек нашёл её у себя."""

    line: int
    values: dict[str, object]

    def text(self, name: str) -> str:
        value = self.values.get(name)
        return str(value) if value is not None else ""

    def number(self, name: str) -> float | int | None:
        value = self.values.get(name)
        return value if isinstance(value, int | float) else None


class RowError(ValueError):
    """Строка не годится: про неё, а не про файл."""


# ------------------------------------------------------------------- чтение книги


def _cell_text(value: object) -> str:
    """Текст ячейки. Артикул и номер номенклатуры Excel вернёт числом."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _cell_number(value: object, column: str) -> float | None:
    """Число из ячейки, в том числе записанное текстом: «1 234,50»."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RowError(f"{column}: ожидалось число")
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    text = text.rstrip("₽ресшткм³").strip()
    try:
        return float(text)
    except ValueError as error:
        raise RowError(f"{column}: «{value}» — не число") from error


def read_catalog_file(data: bytes) -> list[FileRow]:
    """Разбирает книгу в строки. Ошибки здесь — про файл целиком."""
    if not data:
        raise CatalogImportError("Файл пустой")
    if len(data) > MAX_FILE_BYTES:
        raise CatalogImportError(f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ")
    try:
        workbook = load_workbook(BytesIO(data), data_only=True, read_only=True)
    except Exception as error:  # noqa: BLE001 — openpyxl бросает что угодно
        raise CatalogImportError(
            "Файл не открывается как книга Excel (.xlsx). Из Google Sheets выгружайте "
            "именно .xlsx, а не .csv"
        ) from error

    try:
        sheet = workbook.worksheets[0]
        grid = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()

    header_line, mapping = _find_header(grid)
    rows: list[FileRow] = []
    for number, raw in enumerate(grid[header_line + 1 :], start=header_line + 2):
        values: dict[str, object] = {}
        for index, field_name in mapping.items():
            if index >= len(raw):
                continue
            cell = raw[index]
            if field_name in TEXT_FIELDS:
                text = _cell_text(cell)
                if text:
                    values[field_name] = text
            else:
                value = _cell_number(cell, field_name)
                if value is not None:
                    values[field_name] = int(value) if field_name in INT_FIELDS else value
        if not values.get("vendor_code"):
            # Пустая строка или подпись под таблицей: без артикула брать нечего.
            continue
        rows.append(FileRow(line=number, values=values))
        if len(rows) > MAX_ROWS:
            raise CatalogImportError(f"В файле больше {MAX_ROWS} строк — загрузите его частями")
    if not rows:
        raise CatalogImportError(
            "В файле нет ни одной строки с артикулом. Проверьте, что данные лежат "
            "под строкой заголовков, а не на другом листе"
        )
    return rows


def _find_header(grid: list[list[object]]) -> tuple[int, dict[int, str]]:
    """Ищет строку заголовков в первых строках и сопоставляет колонки по названию."""
    for index, raw in enumerate(grid[:20]):
        mapping: dict[int, str] = {}
        for column, cell in enumerate(raw):
            name = _cell_text(cell).lower().replace("\n", " ").strip()
            if name in COLUMNS and COLUMNS[name] not in mapping.values():
                mapping[column] = COLUMNS[name]
        if "vendor_code" in mapping.values():
            return index, mapping
    raise CatalogImportError(
        f"В файле не нашлась строка заголовков с колонкой «{REQUIRED_COLUMN.capitalize()}». "
        "Скачайте шаблон кнопкой рядом и заполните его — названия колонок система ищет "
        "по шапке, а порядок и лишние колонки значения не имеют"
    )


# --------------------------------------------------------------------- разбор строк


@dataclass
class Resolved:
    """Строка, разобранная до объектов базы."""

    row: FileRow
    vendor_code: str
    product: Product | None
    store: Store | None
    pair: StoreProduct | None
    category: Category | None
    category_name: str
    niche: str
    supplier: Supplier | None
    supplier_name: str


def _resolve(session: Session, row: FileRow) -> Resolved:
    """Строка до объектов базы. Товар, заведённый предыдущей строкой файла, тоже
    находится здесь: каждая запись сервиса завершается коммитом."""
    vendor_code = row.text("vendor_code")
    product = session.scalar(
        select(Product).where(func.lower(Product.vendor_code) == vendor_code.lower())
    )

    category_name = row.text("category")
    category = (
        session.scalar(select(Category).where(func.lower(Category.name) == category_name.lower()))
        if category_name
        else None
    )
    supplier_name = row.text("supplier")
    supplier = (
        session.scalar(select(Supplier).where(func.lower(Supplier.name) == supplier_name.lower()))
        if supplier_name
        else None
    )

    store_name = row.text("store")
    store = None
    if store_name:
        store = session.scalar(
            select(Store).where(
                (func.lower(Store.code) == store_name.lower())
                | (func.lower(Store.name) == store_name.lower())
            )
        )
        if store is None:
            raise RowError(
                f"кабинет «{store_name}» не заведён. Кабинеты файл не создаёт: за каждым "
                "стоит ключ API. Заведите его на вкладке справочников и повторите загрузку"
            )

    pair = None
    if store is not None and product is not None and product.id is not None:
        pair = session.scalar(
            select(StoreProduct).where(
                StoreProduct.store_id == store.id, StoreProduct.product_id == product.id
            )
        )
    return Resolved(
        row=row,
        vendor_code=vendor_code,
        product=product,
        store=store,
        pair=pair,
        category=category,
        category_name=category_name,
        niche=row.text("niche"),
        supplier=supplier,
        supplier_name=supplier_name,
    )


def _product_changes(found: Resolved) -> list[str]:
    """Чем файл отличается от карточки. Пустая ячейка не считается отличием."""
    product = found.product
    assert product is not None
    changes: list[str] = []
    for name, label in PRODUCT_FIELDS.items():
        value = found.row.values.get(name)
        if value is None:
            continue
        current = getattr(product, name)
        if isinstance(value, int | float) and isinstance(current, int | float):
            if abs(float(current) - float(value)) < 1e-9:
                continue
        elif str(current or "") == str(value):
            continue
        changes.append(label)
    if found.category_name and product.category_id != (
        found.category.id if found.category else None
    ):
        changes.append("категория")
    if found.supplier_name and product.supplier_id != (
        found.supplier.id if found.supplier else None
    ):
        changes.append("поставщик")
    return changes


def _price_change(found: Resolved) -> str | None:
    """Разойдутся ли цены файла с действующими значениями карточки."""
    product = found.product
    cost = found.row.number("cost_price")
    price = found.row.number("price")
    if cost is None and price is None:
        return None
    if product is None or product.id is None:
        first = cost if cost is not None else price
        return f"{found.vendor_code}: {first:g} ₽"
    same_cost = cost is None or abs(product.cost_price - cost) < 0.005
    same_price = price is None or abs(product.price - price) < 0.005
    if same_cost and same_price:
        return None
    if cost is not None and not same_cost:
        return f"{found.vendor_code}: себестоимость {product.cost_price:g} → {cost:g} ₽"
    return f"{found.vendor_code}: цена {product.price:g} → {price:g} ₽"


def _pair_changes(found: Resolved) -> list[str]:
    pair = found.pair
    assert pair is not None
    changes: list[str] = []
    nm_id = found.row.text("nm_id")
    if nm_id and (pair.nm_id or "") != nm_id:
        changes.append("номенклатура WB")
    barcode = found.row.text("barcode")
    if barcode and (pair.barcode or "") != barcode:
        changes.append("штрихкод")
    potential = found.row.number("base_potential")
    if potential is not None and abs(pair.base_potential - potential) > 1e-9:
        changes.append("базовый потенциал")
    return changes


# ------------------------------------------------------------------------- разбор


def analyze(session: Session, data: bytes) -> Plan:
    """Что файл изменит. В базу не пишет ничего."""
    rows = read_catalog_file(data)
    plan = Plan(rows_read=len(rows))

    seen_products: set[str] = set()
    seen_categories: set[str] = set()
    seen_suppliers: set[str] = set()
    seen_pairs: set[tuple[str, int]] = set()
    #: Цена пишется один раз на товар, сколько бы строк (кабинетов) у него ни было.
    priced: set[str] = set()

    for row in rows:
        try:
            found = _resolve(session, row)
        except RowError as error:
            plan.issues.add(f"строка {row.line}: {error}")
            continue

        code = found.vendor_code.lower()
        if found.category_name and found.category is None:
            key = found.category_name.lower()
            if key not in seen_categories:
                seen_categories.add(key)
                plan.new_categories.add(found.category_name)
        if found.supplier_name and found.supplier is None:
            key = found.supplier_name.lower()
            if key not in seen_suppliers:
                seen_suppliers.add(key)
                plan.new_suppliers.add(found.supplier_name)

        if found.product is None:
            if code not in seen_products:
                if not found.row.text("title"):
                    plan.issues.add(f"строка {row.line}: новому товару нужно название")
                    continue
                seen_products.add(code)
                plan.new_products.add(f"{found.vendor_code} · {found.row.text('title')}")
        elif code not in seen_products:
            seen_products.add(code)
            changes = _product_changes(found)
            if changes:
                plan.updated_products.add(f"{found.vendor_code}: {', '.join(changes)}")

        if code not in priced:
            priced.add(code)
            price = _price_change(found)
            if price is not None:
                plan.price_records.add(price)

        if found.store is None:
            continue
        pair_key = (code, found.store.id)
        if pair_key in seen_pairs:
            plan.issues.add(
                f"строка {row.line}: {found.vendor_code} в кабинете {found.store.code} "
                "уже встречался выше — вторая строка пропущена"
            )
            continue
        seen_pairs.add(pair_key)

        if found.pair is None:
            try:
                _check_nm_id_free(session, found)
            except RowError as error:
                plan.issues.add(f"строка {row.line}: {error}")
                continue
            plan.new_pairs.add(f"{found.vendor_code} → {found.store.name}")
        else:
            changes = _pair_changes(found)
            if changes:
                plan.updated_pairs.add(
                    f"{found.vendor_code} → {found.store.name}: {', '.join(changes)}"
                )
    return plan


def _check_nm_id_free(session: Session, found: Resolved) -> None:
    nm_id = found.row.text("nm_id")
    if not nm_id or found.store is None:
        return
    taken = session.scalar(
        select(StoreProduct.id).where(
            StoreProduct.store_id == found.store.id, StoreProduct.nm_id == nm_id
        )
    )
    if taken is not None and (found.pair is None or taken != found.pair.id):
        raise RowError(
            f"номенклатура {nm_id} в кабинете {found.store.code} уже привязана " "к другому товару"
        )


# ---------------------------------------------------------------------- применение


def apply_import(session: Session, data: bytes, author: str | None = None) -> Plan:
    """Заводит и правит каталог по файлу. Пишет через сервисы справочников и цен."""
    rows = read_catalog_file(data)
    plan = Plan(rows_read=len(rows), applied=True)
    today = date.today()

    priced: set[str] = set()
    seen_pairs: set[tuple[str, int]] = set()
    #: Артикулы, карточку которых файл уже разобрал: у товара в трёх кабинетах
    #: три строки, и сверять карточку с файлом трижды незачем.
    handled: set[str] = set()

    for row in rows:
        try:
            found = _resolve(session, row)
            _apply_row(session, found, plan, priced, seen_pairs, handled, author, today)
        except RowError as error:
            plan.issues.add(f"строка {row.line}: {error}")
        except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
            plan.issues.add(f"строка {row.line}: {error}")
    return plan


def _apply_row(
    session: Session,
    found: Resolved,
    plan: Plan,
    priced: set[str],
    seen_pairs: set[tuple[str, int]],
    handled: set[str],
    author: str | None,
    today: date,
) -> None:
    row = found.row
    code = found.vendor_code.lower()
    # Расхождение цен считается до заведения товара: у новой карточки
    # себестоимость нулевая, и «0 → 260 ₽» выглядело бы так, будто ноль
    # был в файле. Для нового товара это просто «260 ₽».
    described = _price_change(found)

    if found.category_name and found.category is None:
        found.category = catalog_service.create_category(
            session, name=found.category_name, niche=found.niche or None
        )
        plan.new_categories.add(found.category.name)
    if found.supplier_name and found.supplier is None:
        found.supplier = catalog_service.create_supplier(session, name=found.supplier_name)
        plan.new_suppliers.add(found.supplier.name)

    if found.product is None:
        title = row.text("title")
        if not title:
            raise RowError("новому товару нужно название")
        found.product = catalog_service.create_product(
            session,
            vendor_code=found.vendor_code,
            title=title,
            category_id=found.category.id if found.category else None,
            supplier_id=found.supplier.id if found.supplier else None,
            volume_m3=row.number("volume_m3") or 0.0,
            lead_time_days=row.values.get("lead_time_days"),  # type: ignore[arg-type]
            moq=row.values.get("moq"),  # type: ignore[arg-type]
            pack_size=row.values.get("pack_size"),  # type: ignore[arg-type]
            coverage_weeks=row.values.get("coverage_weeks"),  # type: ignore[arg-type]
            safety_weeks=row.number("safety_weeks"),
        )
        plan.new_products.add(f"{found.vendor_code} · {title}")
    elif code not in handled:
        changes = _product_changes(found)
        if changes:
            catalog_service.update_product(
                session,
                found.product,
                title=row.text("title") or None,
                category_id=found.category.id if found.category else None,
                supplier_id=found.supplier.id if found.supplier else None,
                volume_m3=row.number("volume_m3"),
                lead_time_days=row.values.get("lead_time_days"),  # type: ignore[arg-type]
                moq=row.values.get("moq"),  # type: ignore[arg-type]
                pack_size=row.values.get("pack_size"),  # type: ignore[arg-type]
                coverage_weeks=row.values.get("coverage_weeks"),  # type: ignore[arg-type]
                safety_weeks=row.number("safety_weeks"),
            )
            plan.updated_products.add(f"{found.vendor_code}: {', '.join(changes)}")
    handled.add(code)

    if code not in priced:
        priced.add(code)
        if described is not None:
            try:
                price_service.set_price(
                    session,
                    found.product,
                    valid_from=today,
                    cost_price=row.number("cost_price"),
                    price=row.number("price"),
                    author=author,
                    note=PRICE_NOTE,
                    today=today,
                )
                plan.price_records.add(described)
            except price_service.PriceError as error:
                # Например, на сегодняшнюю дату уже записана та же цена.
                plan.issues.add(f"строка {row.line}: цена не записана — {error}")

    if found.store is None:
        return
    pair_key = (code, found.store.id)
    if pair_key in seen_pairs:
        raise RowError(
            f"{found.vendor_code} в кабинете {found.store.code} уже встречался выше — "
            "вторая строка пропущена"
        )
    seen_pairs.add(pair_key)

    if found.pair is None:
        _check_nm_id_free(session, found)
        catalog_service.add_pair(
            session,
            product=found.product,
            store_id=found.store.id,
            nm_id=row.text("nm_id") or None,
            barcode=row.text("barcode") or None,
            base_potential=row.number("base_potential") or 0.0,
        )
        plan.new_pairs.add(f"{found.vendor_code} → {found.store.name}")
    else:
        changes = _pair_changes(found)
        if changes:
            catalog_service.update_pair(
                session,
                found.pair,
                nm_id=row.text("nm_id") or None,
                barcode=row.text("barcode") or None,
                base_potential=row.number("base_potential"),
            )
            plan.updated_pairs.add(
                f"{found.vendor_code} → {found.store.name}: {', '.join(changes)}"
            )
