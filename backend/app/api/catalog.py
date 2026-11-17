"""Справочники: чтение и ведение (задачи B-8, B-9, B-14).

Писать в справочники может редактор — тот же, кто правит прогноз и собирает
заказы: номенклатуру ведёт снабжение, а не администратор системы. Права на
управление людьми тут не нужны, права на правку данных — нужны.

Коды ответов: 404 — нет такой записи, 409 — значение занято (код кабинета,
артикул, пара, номенклатура кабинета), 422 — отказ по смыслу (удалить то,
на что ссылаются, снять несуществующее поле).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi import status as http_status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..models import Category, Product, Store, StoreProduct, Supplier, User, WeekFact
from ..observability import event
from ..schemas import (
    BindIn,
    CatalogImportOut,
    CategoryIn,
    CategoryOut,
    CategoryUpdateIn,
    DuplicateHintOut,
    MergeIn,
    MergeOut,
    PairIn,
    PairOut,
    PairUpdateIn,
    PriceCardOut,
    PriceIn,
    PriceOut,
    ProductIn,
    ProductOut,
    ProductUpdateIn,
    SettingsOut,
    StoreIn,
    StoreOut,
    StoreUpdateIn,
    SupplierIn,
    SupplierOut,
    SupplierUpdateIn,
    UnmatchedOut,
)
from ..services import catalog as catalog_service
from ..services import catalog_import, matching
from ..services import prices as price_service
from ..services.catalog_export import build_catalog_workbook, catalog_filename
from ..services.matrix import load_app_settings
from .deps import require_editor
from .responses import xlsx_response

router = APIRouter(prefix="/catalog", tags=["Справочники"])


def _refusal(error: Exception) -> HTTPException:
    """Отказ сервиса — человеку, как он написан; код — по виду отказа."""
    code = 409 if isinstance(error, catalog_service.CatalogConflict) else 422
    return HTTPException(status_code=code, detail=str(error))


def _store_out(store: Store) -> StoreOut:
    return StoreOut(
        id=store.id,
        code=store.code,
        name=store.name,
        marketplace=store.marketplace.value,
        is_active=store.is_active,
    )


@router.get("/stores", response_model=list[StoreOut], summary="Кабинеты")
def list_stores(session: Session = Depends(get_session)) -> list[StoreOut]:
    stores = session.scalars(select(Store).order_by(Store.code)).all()
    return [_store_out(store) for store in stores]


@router.get("/categories", response_model=list[CategoryOut], summary="Категории")
def list_categories(session: Session = Depends(get_session)) -> list[CategoryOut]:
    rows = session.scalars(select(Category).order_by(Category.name)).all()
    return [CategoryOut.model_validate(row) for row in rows]


@router.get("/suppliers", response_model=list[SupplierOut], summary="Поставщики")
def list_suppliers(session: Session = Depends(get_session)) -> list[SupplierOut]:
    rows = session.scalars(select(Supplier).order_by(Supplier.name)).all()
    counts = dict(
        session.execute(
            select(Product.supplier_id, func.count(Product.id)).group_by(Product.supplier_id)
        ).all()
    )
    return [
        SupplierOut(
            id=row.id,
            name=row.name,
            lead_time_days=row.lead_time_days,
            moq=row.moq,
            product_count=counts.get(row.id, 0),
        )
        for row in rows
    ]


def _products_out(session: Session, products: list[Product]) -> list[ProductOut]:
    ids = [p.id for p in products]
    since = price_service.valid_from_by_product(session, ids)
    pairs = dict(
        session.execute(
            select(StoreProduct.product_id, func.count(StoreProduct.id))
            .where(StoreProduct.product_id.in_(ids))
            .group_by(StoreProduct.product_id)
        ).all()
    )
    return [
        ProductOut(
            id=p.id,
            vendor_code=p.vendor_code,
            title=p.title,
            category=p.category.name if p.category else None,
            supplier=p.supplier.name if p.supplier else None,
            cost_price=p.cost_price,
            price=p.price,
            volume_m3=p.volume_m3,
            lead_time_days=p.lead_time_days,
            moq=p.moq,
            pack_size=p.pack_size,
            price_valid_from=since.get(p.id),
            category_id=p.category_id,
            supplier_id=p.supplier_id,
            coverage_weeks=p.coverage_weeks,
            safety_weeks=p.safety_weeks,
            is_active=p.is_active,
            store_count=pairs.get(p.id, 0),
        )
        for p in products
    ]


def _one_product_out(session: Session, product: Product) -> ProductOut:
    fresh = session.scalars(
        select(Product)
        .options(selectinload(Product.category), selectinload(Product.supplier))
        .where(Product.id == product.id)
    ).one()
    return _products_out(session, [fresh])[0]


@router.get("/products", response_model=list[ProductOut], summary="Товары")
def list_products(
    search: str | None = Query(None),
    session: Session = Depends(get_session),
) -> list[ProductOut]:
    stmt = (
        select(Product)
        .options(selectinload(Product.category), selectinload(Product.supplier))
        .order_by(Product.vendor_code)
    )
    if search:
        pattern = f"%{search.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Product.title).like(pattern) | func.lower(Product.vendor_code).like(pattern)
        )
    return _products_out(session, list(session.scalars(stmt).all()))


def _product_or_404(session: Session, product_id: int) -> Product:
    product = session.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="товар не найден")
    return product


def _price_card(session: Session, product: Product) -> PriceCardOut:
    rows = price_service.history(session, product.id)
    current = price_service.effective(rows, date.today())
    return PriceCardOut(
        product_id=product.id,
        vendor_code=product.vendor_code,
        title=product.title,
        supplier=product.supplier.name if product.supplier else None,
        cost_price=product.cost_price,
        price=product.price,
        valid_from=current.valid_from if current else None,
        history=[PriceOut.model_validate(row) for row in rows],
    )


@router.get(
    "/products/{product_id}/prices",
    response_model=PriceCardOut,
    summary="История цен товара",
)
def list_prices(product_id: int, session: Session = Depends(get_session)) -> PriceCardOut:
    """Нынешние цены и вся череда изменений, от новой к старой (задача B-9)."""
    return _price_card(session, _product_or_404(session, product_id))


@router.post(
    "/products/{product_id}/prices",
    response_model=PriceCardOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Записать новую цену товара",
)
def add_price(
    product_id: int,
    payload: PriceIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> PriceCardOut:
    """Цена с даты. Если дата уже занята, запись на ней переписывается."""
    product = _product_or_404(session, product_id)
    try:
        price_service.set_price(
            session,
            product,
            valid_from=payload.valid_from,
            cost_price=payload.cost_price,
            price=payload.price,
            author=user.display_name,
            note=payload.note,
        )
    except price_service.PriceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _price_card(session, product)


@router.delete(
    "/products/{product_id}/prices/{price_id}",
    response_model=PriceCardOut,
    summary="Убрать запись из истории цен",
)
def delete_price(
    product_id: int,
    price_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> PriceCardOut:
    """Для ошибочной записи: дата или сумма введены неверно.

    Карточка товара после этого получает то, что действует без убранной записи.
    """
    product = _product_or_404(session, product_id)
    row = price_service.find_row(session, product_id, price_id)
    if row is None:
        raise HTTPException(status_code=404, detail="запись истории цен не найдена")
    price_service.delete_price(session, product, row)
    return _price_card(session, product)


# ------------------------------------------- ведение справочников (задача B-14)


def _store_or_404(session: Session, store_id: int) -> Store:
    store = session.get(Store, store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="кабинет не найден")
    return store


def _supplier_or_404(session: Session, supplier_id: int) -> Supplier:
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=404, detail="поставщик не найден")
    return supplier


def _category_or_404(session: Session, category_id: int) -> Category:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="категория не найдена")
    return category


def _pair_or_404(session: Session, pair_id: int) -> StoreProduct:
    pair = session.get(StoreProduct, pair_id)
    if pair is None:
        raise HTTPException(status_code=404, detail="пара магазин/товар не найдена")
    return pair


def _no_content() -> Response:
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


def _pairs_out(session: Session, pairs: list[StoreProduct]) -> list[PairOut]:
    ids = [pair.id for pair in pairs]
    facts = dict(
        session.execute(
            select(WeekFact.store_product_id, func.count(WeekFact.id))
            .where(WeekFact.store_product_id.in_(ids))
            .group_by(WeekFact.store_product_id)
        ).all()
    )
    stores = {
        store.id: store
        for store in session.scalars(
            select(Store).where(Store.id.in_([pair.store_id for pair in pairs]))
        ).all()
    }
    out = []
    for pair in pairs:
        store = stores[pair.store_id]
        out.append(
            PairOut(
                id=pair.id,
                store_id=store.id,
                store_code=store.code,
                store_name=store.name,
                store_active=store.is_active,
                nm_id=pair.nm_id,
                barcode=pair.barcode,
                base_potential=pair.base_potential,
                is_active=pair.is_active,
                fact_count=facts.get(pair.id, 0),
            )
        )
    return out


@router.post(
    "/stores",
    response_model=StoreOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Завести кабинет",
)
def create_store(
    payload: StoreIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> StoreOut:
    """Код кабинета — то, по чему в `WB_TOKENS` ищется ключ API."""
    try:
        store = catalog_service.create_store(
            session,
            code=payload.code,
            name=payload.name,
            marketplace=payload.marketplace,
            cabinet_ref=payload.cabinet_ref,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _store_out(store)


@router.put("/stores/{store_id}", response_model=StoreOut, summary="Изменить кабинет")
def update_store(
    store_id: int,
    payload: StoreUpdateIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> StoreOut:
    """Отключённый кабинет выпадает из расчёта вместе со своими позициями."""
    store = _store_or_404(session, store_id)
    try:
        catalog_service.update_store(
            session,
            store,
            name=payload.name,
            marketplace=payload.marketplace,
            cabinet_ref=payload.cabinet_ref,
            is_active=payload.is_active,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _store_out(store)


@router.delete(
    "/stores/{store_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Удалить кабинет",
)
def delete_store(
    store_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> Response:
    """Только пока в кабинете нет товаров: иначе отключают, а не удаляют."""
    store = _store_or_404(session, store_id)
    try:
        catalog_service.delete_store(session, store)
    except catalog_service.CatalogError as error:
        raise _refusal(error) from error
    return _no_content()


@router.post(
    "/suppliers",
    response_model=SupplierOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Завести поставщика",
)
def create_supplier(
    payload: SupplierIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> SupplierOut:
    try:
        supplier = catalog_service.create_supplier(
            session, name=payload.name, lead_time_days=payload.lead_time_days, moq=payload.moq
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return SupplierOut.model_validate(supplier)


@router.put("/suppliers/{supplier_id}", response_model=SupplierOut, summary="Изменить поставщика")
def update_supplier(
    supplier_id: int,
    payload: SupplierUpdateIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> SupplierOut:
    """Срок поставки и MOQ сразу меняют дату и объём заказа по его товарам."""
    supplier = _supplier_or_404(session, supplier_id)
    try:
        catalog_service.update_supplier(
            session,
            supplier,
            name=payload.name,
            lead_time_days=payload.lead_time_days,
            moq=payload.moq,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return SupplierOut.model_validate(supplier)


@router.delete(
    "/suppliers/{supplier_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Удалить поставщика",
)
def delete_supplier(
    supplier_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> Response:
    supplier = _supplier_or_404(session, supplier_id)
    try:
        catalog_service.delete_supplier(session, supplier)
    except catalog_service.CatalogError as error:
        raise _refusal(error) from error
    return _no_content()


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Завести категорию",
)
def create_category(
    payload: CategoryIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> CategoryOut:
    """Коэффициенты сезонности задаются потом, на своём экране."""
    try:
        category = catalog_service.create_category(
            session, name=payload.name, niche=payload.niche, abc_class=payload.abc_class
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return CategoryOut.model_validate(category)


@router.put("/categories/{category_id}", response_model=CategoryOut, summary="Изменить категорию")
def update_category(
    category_id: int,
    payload: CategoryUpdateIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> CategoryOut:
    category = _category_or_404(session, category_id)
    try:
        catalog_service.update_category(
            session,
            category,
            name=payload.name,
            niche=payload.niche,
            abc_class=payload.abc_class,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return CategoryOut.model_validate(category)


@router.delete(
    "/categories/{category_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Удалить категорию",
)
def delete_category(
    category_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> Response:
    category = _category_or_404(session, category_id)
    try:
        catalog_service.delete_category(session, category)
    except catalog_service.CatalogError as error:
        raise _refusal(error) from error
    return _no_content()


@router.post(
    "/products",
    response_model=ProductOut,
    status_code=http_status.HTTP_201_CREATED,
    summary="Завести товар",
)
def create_product(
    payload: ProductIn,
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> ProductOut:
    """Номенклатура. Начальная цена уходит в историю первой записью (задача B-9).

    Считаться товар начнёт после того, как его завели в кабинет: все недельные
    данные привязаны к паре (магазин, товар).
    """
    try:
        product = catalog_service.create_product(
            session,
            vendor_code=payload.vendor_code,
            title=payload.title,
            category_id=payload.category_id,
            supplier_id=payload.supplier_id,
            volume_m3=payload.volume_m3,
            lead_time_days=payload.lead_time_days,
            moq=payload.moq,
            pack_size=payload.pack_size,
            coverage_weeks=payload.coverage_weeks,
            safety_weeks=payload.safety_weeks,
            cost_price=payload.cost_price,
            price=payload.price,
            author=user.display_name,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    except price_service.PriceError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _one_product_out(session, product)


@router.put("/products/{product_id}", response_model=ProductOut, summary="Изменить товар")
def update_product(
    product_id: int,
    payload: ProductUpdateIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> ProductOut:
    """Без цен: их пишет только сервис цен, иначе карточка разойдётся с историей."""
    product = _product_or_404(session, product_id)
    try:
        catalog_service.update_product(
            session,
            product,
            vendor_code=payload.vendor_code,
            title=payload.title,
            category_id=payload.category_id,
            supplier_id=payload.supplier_id,
            volume_m3=payload.volume_m3,
            lead_time_days=payload.lead_time_days,
            moq=payload.moq,
            pack_size=payload.pack_size,
            coverage_weeks=payload.coverage_weeks,
            safety_weeks=payload.safety_weeks,
            is_active=payload.is_active,
            clear=set(payload.clear),
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _one_product_out(session, product)


@router.delete(
    "/products/{product_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Удалить товар",
)
def delete_product(
    product_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> Response:
    """Вместе с историей цен — она без товара бессмысленна.

    Товар, заведённый хотя бы в один кабинет, не удаляется: за парой стоят
    продажи. Его снимают с продажи.
    """
    product = _product_or_404(session, product_id)
    try:
        catalog_service.delete_product(session, product)
    except catalog_service.CatalogError as error:
        raise _refusal(error) from error
    return _no_content()


@router.get(
    "/products/{product_id}/stores",
    response_model=list[PairOut],
    summary="В каких кабинетах товар заведён",
)
def list_product_stores(product_id: int, session: Session = Depends(get_session)) -> list[PairOut]:
    _product_or_404(session, product_id)
    return _pairs_out(session, catalog_service.pairs_of(session, product_id))


@router.post(
    "/products/{product_id}/stores",
    response_model=list[PairOut],
    status_code=http_status.HTTP_201_CREATED,
    summary="Завести товар в кабинет",
)
def add_product_store(
    product_id: int,
    payload: PairIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> list[PairOut]:
    """Пара (магазин, товар) — то, что считается. Отдаётся весь список пар товара."""
    product = _product_or_404(session, product_id)
    try:
        catalog_service.add_pair(
            session,
            product=product,
            store_id=payload.store_id,
            nm_id=payload.nm_id,
            barcode=payload.barcode,
            base_potential=payload.base_potential,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _pairs_out(session, catalog_service.pairs_of(session, product_id))


@router.put("/pairs/{pair_id}", response_model=PairOut, summary="Изменить пару магазин/товар")
def update_pair(
    pair_id: int,
    payload: PairUpdateIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> PairOut:
    """Отключённая пара выпадает из расчёта, её прошлое остаётся в базе."""
    pair = _pair_or_404(session, pair_id)
    try:
        catalog_service.update_pair(
            session,
            pair,
            nm_id=payload.nm_id,
            barcode=payload.barcode,
            base_potential=payload.base_potential,
            is_active=payload.is_active,
        )
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _pairs_out(session, [pair])[0]


@router.delete(
    "/pairs/{pair_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    summary="Удалить пару магазин/товар",
)
def delete_pair(
    pair_id: int,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> Response:
    """Только пока за парой нет фактов, поставок, планов, правок и заказов."""
    pair = _pair_or_404(session, pair_id)
    try:
        catalog_service.delete_pair(session, pair)
    except catalog_service.CatalogError as error:
        raise _refusal(error) from error
    return _no_content()


# ------------------------------------------- сопоставление SKU (задача B-10)


@router.get(
    "/unmatched",
    response_model=list[UnmatchedOut],
    summary="Номенклатуры WB, которых нет в каталоге",
)
def list_unmatched(
    store_id: int | None = Query(None),
    session: Session = Depends(get_session),
) -> list[UnmatchedOut]:
    """Номера, которые загрузка пропустила: их продажи сейчас не считаются."""
    return [UnmatchedOut.model_validate(row) for row in matching.unmatched(session, store_id)]


@router.post(
    "/unmatched/bind",
    response_model=list[PairOut],
    summary="Привязать номенклатуру к товару",
)
def bind_unmatched(
    payload: BindIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> list[PairOut]:
    """Заводит пару с этим номером или проставляет его существующей паре.

    Факты по номеру приедут следующей загрузкой: задним числом система
    их не перезапрашивает.
    """
    try:
        matching.bind(
            session,
            store_id=payload.store_id,
            nm_id=payload.nm_id,
            product_id=payload.product_id,
        )
    except matching.MatchError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (catalog_service.CatalogError, catalog_service.CatalogConflict) as error:
        raise _refusal(error) from error
    return _pairs_out(session, catalog_service.pairs_of(session, payload.product_id))


@router.get(
    "/duplicates",
    response_model=list[DuplicateHintOut],
    summary="Товары, похожие на дубли",
)
def list_duplicates(session: Session = Depends(get_session)) -> list[DuplicateHintOut]:
    """Один товар, заведённый дважды, делит спрос и закупку пополам."""
    return [
        DuplicateHintOut.model_validate(hint) for hint in matching.duplicate_candidates(session)
    ]


@router.post(
    "/products/{product_id}/merge",
    response_model=MergeOut,
    summary="Слить товар в другой",
)
def merge_products(
    product_id: int,
    payload: MergeIn,
    session: Session = Depends(get_session),
    _: User = Depends(require_editor),
) -> MergeOut:
    """Переносит кабинеты исходного товара на целевой вместе с их прошлым.

    Исходный товар не удаляется, а снимается с продажи: у него остаётся своя
    история цен, и убрать её — отдельное решение человека.
    """
    source = _product_or_404(session, product_id)
    target = _product_or_404(session, payload.into)
    try:
        result = matching.merge(session, source=source, target=target)
    except matching.MatchError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return MergeOut.model_validate(result)


# ------------------------------------- загрузка номенклатуры файлом (задача B-15)


@router.get("/export", summary="Выгрузка каталога в XLSX (она же шаблон загрузки)")
def export_catalog(session: Session = Depends(get_session)) -> Response:
    """Весь каталог одной таблицей, в том же виде, в каком принимается загрузка.

    Отдельного пустого шаблона нет: на пустой базе в книге только шапка — это
    и есть шаблон. Шаблон, живущий сам по себе, рано или поздно разошёлся бы
    с тем, что система умеет читать.
    """
    return xlsx_response(
        build_catalog_workbook(session),
        catalog_filename(),
        "catalog.xlsx",
    )


@router.post(
    "/import",
    response_model=CatalogImportOut,
    summary="Загрузка номенклатуры из файла",
)
def import_catalog(
    file: UploadFile = File(..., description="Книга с номенклатурой"),
    apply: bool = Query(False, description="false — только разбор, в базу ничего не пишется"),
    session: Session = Depends(get_session),
    user: User = Depends(require_editor),
) -> CatalogImportOut:
    """Заводит из файла товары, категории, поставщиков и пары (магазин, товар).

    По умолчанию ничего не пишет: сначала человек видит, что именно изменится,
    и только потом подтверждает. Файл приходит дважды — разбор и применение
    независимы, и сервер между ними ничего не помнит.

    Пишет не эта ручка, а сервисы справочников и цен: загрузка не должна
    обходить правила, ради которых они существуют.
    """
    data = file.file.read()
    try:
        plan = (
            catalog_import.apply_import(session, data, author=user.display_name)
            if apply
            else catalog_import.analyze(session, data)
        )
    except catalog_import.CatalogImportError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if apply:
        # Разбор не логируем: он ничего не меняет, а строка в логе должна
        # означать «в каталоге что-то произошло».
        event(
            "загрузка номенклатуры",
            rows=plan.rows_read,
            products=plan.new_products.count,
            pairs=plan.new_pairs.count,
            prices=plan.price_records.count,
            skipped=plan.issues.count,
        )
    return CatalogImportOut.model_validate(plan)


@router.get("/settings", response_model=SettingsOut, summary="Настройки расчёта")
def get_calc_settings(session: Session = Depends(get_session)) -> SettingsOut:
    row = load_app_settings(session)
    return SettingsOut(
        last_closed_week=row.last_closed_week,
        horizon_weeks=row.horizon_weeks,
        history_weeks=row.history_weeks,
        over_coverage_weeks=row.over_coverage_weeks,
        short_horizon_weeks=row.short_horizon_weeks,
        demand_avg_weeks=row.demand_avg_weeks,
        default_lead_time_days=row.default_lead_time_days,
        default_moq=row.default_moq,
        default_pack_size=row.default_pack_size,
        default_coverage_weeks=row.default_coverage_weeks,
        default_safety_weeks=row.default_safety_weeks,
        wb_client=get_settings().wb_client,
    )
