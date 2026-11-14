"""Размножение демо-каталога (ключ ``--scale``).

Инструмент для проверки на объёме: виртуализация таблицы и нагрузочные замеры
без него проверяются на восемнадцати строках, то есть никак. Тест сторожит
единственное, что в нём легко сломать, — уникальность артикулов и связность
ассортимента с товарами.
"""

from __future__ import annotations

from app.seed import ASSORTMENT, PRODUCTS, expand_catalog


def test_scale_one_keeps_the_demo_catalog():
    products, assortment = expand_catalog(1)
    assert products == list(PRODUCTS)
    assert assortment == list(ASSORTMENT)


def test_scale_multiplies_and_keeps_codes_unique():
    products, assortment = expand_catalog(60)

    assert len(products) == len(PRODUCTS) * 60
    assert len(assortment) == len(ASSORTMENT) * 60
    codes = [code for code, *_ in products]
    assert len(set(codes)) == len(codes), "повторный артикул упадёт на уникальном индексе"

    # Каждая строка ассортимента ссылается на существующий товар, иначе сид
    # свалится на поиске по словарю товаров.
    assert {vendor for _, vendor, _ in assortment} <= set(codes)

    # Первая копия — исходный каталог: демо-данные внутри большого остаются узнаваемыми.
    assert products[: len(PRODUCTS)] == list(PRODUCTS)
