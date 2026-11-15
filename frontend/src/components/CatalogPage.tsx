import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../api';
import type { Category, Pair, Product, Store, Supplier } from '../types';

/**
 * Экран справочников (задача B-14): кабинеты, поставщики, категории, товары
 * и пары (магазин, товар).
 *
 * До него каталог умел появляться только демонстрационным сидом — вместе
 * с выдуманными продажами, — и развёртывание на живых данных упиралось в то,
 * что завести магазин и товар нечем.
 *
 * Два правила видны прямо на экране. Первое: ничего не удаляется, пока на это
 * ссылаются факты, заказы или товары, — такую запись отключают, и её прошлое
 * остаётся в базе. Второе: цен здесь нет. В действующую цену пишет только
 * экран цен, иначе карточка товара и история цен разойдутся; исключение —
 * начальная цена при заведении товара, она уходит в историю первой записью.
 */

type Section = 'products' | 'stores' | 'suppliers' | 'categories';

const SECTIONS: { id: Section; label: string }[] = [
  { id: 'products', label: 'Товары' },
  { id: 'stores', label: 'Кабинеты' },
  { id: 'suppliers', label: 'Поставщики' },
  { id: 'categories', label: 'Категории' },
];

interface Notice {
  kind: 'ok' | 'bad';
  text: string;
}

interface Props {
  canEdit: boolean;
  onError: (message: string | null) => void;
  /** Правка справочника меняет расчёт: экраны матрицы и закупки перечитываются. */
  onChanged: () => void;
}

export function CatalogPage({ canEdit, onError, onChanged }: Props) {
  const [section, setSection] = useState<Section>('products');
  const [stores, setStores] = useState<Store[]>([]);
  const [suppliers, setSuppliers] = useState<Supplier[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [products, setProducts] = useState<Product[]>([]);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState('');

  /** Пары раскрытого товара: грузятся по требованию, у каждого товара свои. */
  const [openProduct, setOpenProduct] = useState<number | null>(null);
  const [pairs, setPairs] = useState<Pair[]>([]);

  const load = useCallback(async () => {
    try {
      const [s, p, c, pr] = await Promise.all([
        api.stores(),
        api.suppliers(),
        api.categories(),
        api.products(),
      ]);
      setStores(s);
      setSuppliers(p);
      setCategories(c);
      setProducts(pr);
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Любое действие справочника: сообщение об успехе или отказ сервера как есть.
   *
   * Правила «нельзя удалить то, на что ссылаются» и «цену сюда не пишут» живут
   * на сервере, и дублировать их здесь — значит однажды разойтись: экран
   * запретит то, что сервер разрешает, или наоборот.
   */
  const act = useCallback(
    async (job: () => Promise<void>, done: string, affectsCalc = true): Promise<boolean> => {
      setBusy(true);
      try {
        await job();
        setNotice({ kind: 'ok', text: done });
        if (affectsCalc) onChanged();
        return true;
      } catch (err) {
        setNotice({ kind: 'bad', text: (err as Error).message });
        return false;
      } finally {
        setBusy(false);
      }
    },
    [onChanged],
  );

  const openPairs = useCallback(
    async (product: Product) => {
      if (openProduct === product.id) {
        setOpenProduct(null);
        return;
      }
      setOpenProduct(product.id);
      setPairs([]);
      try {
        setPairs(await api.productStores(product.id));
      } catch (err) {
        setNotice({ kind: 'bad', text: (err as Error).message });
      }
    },
    [openProduct],
  );

  // Поиск местный: товары уже загружены целиком, и запрос ради подстроки был бы
  // лишним кругом — как на экране цен.
  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return products;
    return products.filter(
      (p) =>
        p.vendor_code.toLowerCase().includes(needle) || p.title.toLowerCase().includes(needle),
    );
  }, [products, search]);

  const counts = useMemo(
    () => ({
      active: products.filter((p) => p.is_active).length,
      orphans: products.filter((p) => p.store_count === 0).length,
    }),
    [products],
  );

  const refreshProduct = (updated: Product) =>
    setProducts((prev) =>
      prev
        .map((row) => (row.id === updated.id ? updated : row))
        .sort((a, b) => a.vendor_code.localeCompare(b.vendor_code)),
    );

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Справочники</h2>
          <p className="muted">
            Кабинеты, поставщики, категории и номенклатура. Товар начинает считаться
            после того, как его завели в кабинет: все недельные данные привязаны к паре
            (магазин, товар).
          </p>
        </div>
        <div className="panel-totals">
          <span className="muted">
            {products.length} товаров · {counts.active} в продаже
            {counts.orphans > 0 && ` · ${counts.orphans} без кабинета`} · {stores.length} кабинетов
          </span>
        </div>
      </header>

      <div className="level-switch">
        <span className="level-label">Справочник</span>
        {SECTIONS.map((item) => (
          <button
            key={item.id}
            className={section === item.id ? 'active' : ''}
            onClick={() => setSection(item.id)}
          >
            {item.label}
          </button>
        ))}
        {section === 'products' && (
          <input
            className="search"
            placeholder="Товар или артикул"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        )}
      </div>

      {notice && (
        <p className={notice.kind === 'bad' ? 'inline-warning' : 'inline-note'}>{notice.text}</p>
      )}

      {section === 'products' && (
        <ProductsSection
          products={shown}
          categories={categories}
          suppliers={suppliers}
          stores={stores}
          canEdit={canEdit}
          busy={busy}
          openProduct={openProduct}
          pairs={pairs}
          onToggleOpen={(product) => void openPairs(product)}
          onPairs={setPairs}
          act={act}
          onProduct={refreshProduct}
          onReload={() => void load()}
        />
      )}

      {section === 'stores' && (
        <StoresSection
          stores={stores}
          canEdit={canEdit}
          busy={busy}
          act={act}
          onStores={setStores}
          onReload={() => void load()}
        />
      )}

      {section === 'suppliers' && (
        <SuppliersSection
          suppliers={suppliers}
          canEdit={canEdit}
          busy={busy}
          act={act}
          onSuppliers={setSuppliers}
          onReload={() => void load()}
        />
      )}

      {section === 'categories' && (
        <CategoriesSection
          categories={categories}
          canEdit={canEdit}
          busy={busy}
          act={act}
          onCategories={setCategories}
          onReload={() => void load()}
        />
      )}
    </section>
  );
}

type Act = (job: () => Promise<void>, done: string, affectsCalc?: boolean) => Promise<boolean>;

// --------------------------------------------------------------------------- товары

interface ProductsProps {
  products: Product[];
  categories: Category[];
  suppliers: Supplier[];
  stores: Store[];
  canEdit: boolean;
  busy: boolean;
  openProduct: number | null;
  pairs: Pair[];
  onToggleOpen: (product: Product) => void;
  onPairs: (pairs: Pair[]) => void;
  act: Act;
  onProduct: (product: Product) => void;
  onReload: () => void;
}

function ProductsSection({
  products,
  categories,
  suppliers,
  stores,
  canEdit,
  busy,
  openProduct,
  pairs,
  onToggleOpen,
  onPairs,
  act,
  onProduct,
  onReload,
}: ProductsProps) {
  const [vendorCode, setVendorCode] = useState('');
  const [title, setTitle] = useState('');
  const [categoryId, setCategoryId] = useState('');
  const [supplierId, setSupplierId] = useState('');
  const [costPrice, setCostPrice] = useState('');
  const [price, setPrice] = useState('');

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const ok = await act(async () => {
      await api.createProduct({
        vendor_code: vendorCode.trim(),
        title: title.trim(),
        category_id: categoryId ? Number(categoryId) : null,
        supplier_id: supplierId ? Number(supplierId) : null,
        cost_price: costPrice ? Number(costPrice) : null,
        price: price ? Number(price) : null,
      });
    }, `${vendorCode.trim()} заведён. Чтобы он считался, добавьте его в кабинет.`);
    if (ok) {
      setVendorCode('');
      setTitle('');
      setCostPrice('');
      setPrice('');
      onReload();
    }
  };

  return (
    <>
      {canEdit && (
        <form className="price-form" onSubmit={(event) => void create(event)}>
          <label className="inline-field">
            Артикул
            <input value={vendorCode} onChange={(e) => setVendorCode(e.target.value)} placeholder="SKU-001" />
          </label>
          <label className="inline-field grow">
            Название
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Как в заказе поставщику" />
          </label>
          <label className="inline-field">
            Категория
            <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)}>
              <option value="">без категории</option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Поставщик
            <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}>
              <option value="">без поставщика</option>
              {suppliers.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Себестоимость
            <input value={costPrice} onChange={(e) => setCostPrice(e.target.value)} placeholder="₽" />
          </label>
          <label className="inline-field">
            Цена
            <input value={price} onChange={(e) => setPrice(e.target.value)} placeholder="₽" />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            Завести
          </button>
        </form>
      )}

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Артикул</th>
              <th>Название</th>
              <th>Категория</th>
              <th>Поставщик</th>
              {/* Цена здесь только показана: правит её экран цен, и повторять
                  это в каждой строке — двенадцать раз одно и то же. */}
              <th>Себестоимость · экран цен</th>
              <th>Кабинеты</th>
              <th>Продажа</th>
            </tr>
          </thead>
          <tbody>
            {products.map((product) => (
              <Fragment key={product.id}>
                <tr className={product.is_active ? undefined : 'muted-row'}>
                  <td>
                    {canEdit ? (
                      <InlineText
                        value={product.vendor_code}
                        disabled={busy}
                        onCommit={(next) =>
                          act(
                            async () => onProduct(await api.updateProduct(product.id, { vendor_code: next })),
                            `Артикул изменён на ${next}`,
                          )
                        }
                      />
                    ) : (
                      product.vendor_code
                    )}
                  </td>
                  <td>
                    {canEdit ? (
                      <InlineText
                        value={product.title}
                        disabled={busy}
                        onCommit={(next) =>
                          act(
                            async () => onProduct(await api.updateProduct(product.id, { title: next })),
                            'Название изменено',
                          )
                        }
                      />
                    ) : (
                      product.title
                    )}
                  </td>
                  <td>
                    <select
                      value={product.category_id ?? ''}
                      disabled={!canEdit || busy}
                      onChange={(event) =>
                        void act(
                          async () =>
                            onProduct(
                              await api.updateProduct(
                                product.id,
                                event.target.value
                                  ? { category_id: Number(event.target.value) }
                                  : { clear: ['category_id'] },
                              ),
                            ),
                          'Категория изменена: коэффициенты сезонности теперь берутся от неё',
                        )
                      }
                    >
                      <option value="">без категории</option>
                      {categories.map((c) => (
                        <option key={c.id} value={c.id}>
                          {c.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <select
                      value={product.supplier_id ?? ''}
                      disabled={!canEdit || busy}
                      onChange={(event) =>
                        void act(
                          async () =>
                            onProduct(
                              await api.updateProduct(
                                product.id,
                                event.target.value
                                  ? { supplier_id: Number(event.target.value) }
                                  : { clear: ['supplier_id'] },
                              ),
                            ),
                          'Поставщик изменён: срок поставки и MOQ теперь его',
                        )
                      }
                    >
                      <option value="">без поставщика</option>
                      {suppliers.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="num">{product.cost_price.toLocaleString('ru-RU')} ₽</td>
                  <td>
                    <button className="link" onClick={() => onToggleOpen(product)}>
                      {product.store_count > 0
                        ? `${product.store_count} шт.`
                        : 'не заведён нигде'}
                    </button>
                  </td>
                  <td>
                    {canEdit && (
                      <button
                        className="ghost small"
                        disabled={busy}
                        onClick={() =>
                          void act(
                            async () =>
                              onProduct(
                                await api.updateProduct(product.id, { is_active: !product.is_active }),
                              ),
                            product.is_active
                              ? `${product.vendor_code} снят с продажи: позиции выпали из расчёта`
                              : `${product.vendor_code} снова в продаже`,
                          )
                        }
                      >
                        {product.is_active ? 'Снять' : 'Вернуть'}
                      </button>
                    )}
                    {canEdit && product.store_count === 0 && (
                      <button
                        className="link danger"
                        disabled={busy}
                        onClick={() =>
                          void act(async () => {
                            await api.deleteProduct(product.id);
                            onReload();
                          }, `${product.vendor_code} удалён вместе с историей цен`)
                        }
                      >
                        удалить
                      </button>
                    )}
                  </td>
                </tr>
                {openProduct === product.id && (
                  <tr className="detail-row">
                    <td colSpan={7}>
                      <PairsBlock
                        product={product}
                        pairs={pairs}
                        stores={stores}
                        canEdit={canEdit}
                        busy={busy}
                        act={act}
                        onPairs={onPairs}
                        onReload={onReload}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {products.length === 0 && (
              <tr>
                <td className="empty" colSpan={7}>
                  Товаров нет
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Товар без кабинета нигде не считается: заведите его в кабинет и задайте номер
        номенклатуры — по нему загрузка WB находит, куда класть продажи. Базовый потенциал
        задаётся при заведении пары, дальше его правят на экране потенциала. Себестоимость
        и цена живут на экране цен: писать в них умеет только он, иначе карточка товара
        и история цен разойдутся. Товар с кабинетами не удаляется — его снимают с продажи,
        и тогда позиции выпадают из расчёта, а продажи остаются.
      </p>
    </>
  );
}

// ----------------------------------------------------------- пары (магазин, товар)

interface PairsProps {
  product: Product;
  pairs: Pair[];
  stores: Store[];
  canEdit: boolean;
  busy: boolean;
  act: Act;
  onPairs: (pairs: Pair[]) => void;
  onReload: () => void;
}

function PairsBlock({ product, pairs, stores, canEdit, busy, act, onPairs, onReload }: PairsProps) {
  const [storeId, setStoreId] = useState('');
  const [nmId, setNmId] = useState('');
  const [potential, setPotential] = useState('');

  const free = stores.filter((store) => !pairs.some((pair) => pair.store_id === store.id));

  const add = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!storeId) return;
    const ok = await act(async () => {
      onPairs(
        await api.addProductStore(product.id, {
          store_id: Number(storeId),
          nm_id: nmId.trim() || null,
          base_potential: potential ? Number(potential) : 0,
        }),
      );
      onReload();
    }, `${product.vendor_code} заведён в кабинет: позиция появилась в расчёте`);
    if (ok) {
      setStoreId('');
      setNmId('');
      setPotential('');
    }
  };

  return (
    <div className="price-history">
      <table className="purchase inner">
        <thead>
          <tr>
            <th>Кабинет</th>
            <th>Номенклатура WB</th>
            <th>Базовый потенциал</th>
            <th>Фактов</th>
            <th>Расчёт</th>
          </tr>
        </thead>
        <tbody>
          {pairs.map((pair) => (
            <tr key={pair.id} className={pair.is_active && pair.store_active ? undefined : 'muted-row'}>
              <td>
                {pair.store_name}
                {!pair.store_active && <span className="badge">кабинет отключён</span>}
              </td>
              <td>
                {canEdit ? (
                  <InlineText
                    value={pair.nm_id ?? ''}
                    allowEmpty
                    placeholder="nmId"
                    disabled={busy}
                    onCommit={(next) =>
                      act(async () => {
                        const updated = await api.updatePair(pair.id, { nm_id: next || null });
                        onPairs(pairs.map((row) => (row.id === updated.id ? updated : row)));
                      }, next ? `Номенклатура ${next} привязана` : 'Номер номенклатуры снят', false)
                    }
                  />
                ) : (
                  (pair.nm_id ?? '—')
                )}
              </td>
              <td className="num">{pair.base_potential.toLocaleString('ru-RU')}</td>
              <td className="num">{pair.fact_count}</td>
              <td>
                {canEdit && (
                  <button
                    className="ghost small"
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        const updated = await api.updatePair(pair.id, { is_active: !pair.is_active });
                        onPairs(pairs.map((row) => (row.id === updated.id ? updated : row)));
                      }, pair.is_active
                        ? `${product.vendor_code} в «${pair.store_name}» отключён: позиция вне расчёта`
                        : `${product.vendor_code} в «${pair.store_name}» снова считается`)
                    }
                  >
                    {pair.is_active ? 'Отключить' : 'Включить'}
                  </button>
                )}
                {canEdit && pair.fact_count === 0 && (
                  <button
                    className="link danger"
                    disabled={busy}
                    onClick={() =>
                      void act(async () => {
                        await api.deletePair(pair.id);
                        onPairs(pairs.filter((row) => row.id !== pair.id));
                        onReload();
                      }, 'Пара убрана')
                    }
                  >
                    убрать
                  </button>
                )}
              </td>
            </tr>
          ))}
          {pairs.length === 0 && (
            <tr>
              <td className="empty" colSpan={5}>
                Товар не заведён ни в один кабинет, поэтому нигде не считается
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {canEdit && free.length > 0 && (
        <form className="price-form" onSubmit={(event) => void add(event)}>
          <label className="inline-field">
            Кабинет
            <select value={storeId} onChange={(e) => setStoreId(e.target.value)}>
              <option value="">выберите</option>
              {free.map((store) => (
                <option key={store.id} value={store.id}>
                  {store.name}
                </option>
              ))}
            </select>
          </label>
          <label className="inline-field">
            Номенклатура WB
            <input value={nmId} onChange={(e) => setNmId(e.target.value)} placeholder="nmId" />
          </label>
          <label className="inline-field">
            Базовый потенциал
            <input value={potential} onChange={(e) => setPotential(e.target.value)} placeholder="шт./нед." />
          </label>
          <button className="ghost" type="submit" disabled={busy || !storeId}>
            Завести в кабинет
          </button>
        </form>
      )}

      <p className="hint">
        Номер номенклатуры уникален внутри кабинета: две пары с одним номером загрузка WB
        не смогла бы различить, и факты легли бы на одну из них. Пара с фактами не
        удаляется — её отключают, и прошлое остаётся.
      </p>
    </div>
  );
}

// ------------------------------------------------------------------------- кабинеты

interface StoresProps {
  stores: Store[];
  canEdit: boolean;
  busy: boolean;
  act: Act;
  onStores: (stores: Store[]) => void;
  onReload: () => void;
}

function StoresSection({ stores, canEdit, busy, act, onStores, onReload }: StoresProps) {
  const [code, setCode] = useState('');
  const [name, setName] = useState('');

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const ok = await act(async () => {
      await api.createStore({ code: code.trim(), name: name.trim() });
      onReload();
    }, `Кабинет ${code.trim()} заведён. Ключ API задаётся в окружении: WB_TOKENS.`);
    if (ok) {
      setCode('');
      setName('');
    }
  };

  const patch = (updated: Store) =>
    onStores(stores.map((row) => (row.id === updated.id ? updated : row)));

  return (
    <>
      {canEdit && (
        <form className="price-form" onSubmit={(event) => void create(event)}>
          <label className="inline-field">
            Код кабинета
            <input value={code} onChange={(e) => setCode(e.target.value)} placeholder="store01" />
          </label>
          <label className="inline-field grow">
            Название
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Основной склад WB" />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            Завести
          </button>
        </form>
      )}

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Код</th>
              <th>Название</th>
              <th>Маркетплейс</th>
              <th>Расчёт</th>
            </tr>
          </thead>
          <tbody>
            {stores.map((store) => (
              <tr key={store.id} className={store.is_active ? undefined : 'muted-row'}>
                <td>{store.code}</td>
                <td>
                  {canEdit ? (
                    <InlineText
                      value={store.name}
                      disabled={busy}
                      onCommit={(next) =>
                        act(async () => patch(await api.updateStore(store.id, { name: next })), 'Название изменено', false)
                      }
                    />
                  ) : (
                    store.name
                  )}
                </td>
                <td>{store.marketplace.toUpperCase()}</td>
                <td>
                  {canEdit && (
                    <button
                      className="ghost small"
                      disabled={busy}
                      onClick={() =>
                        void act(
                          async () => patch(await api.updateStore(store.id, { is_active: !store.is_active })),
                          store.is_active
                            ? `${store.name} отключён: его позиции выпали из расчёта`
                            : `${store.name} снова считается`,
                        )
                      }
                    >
                      {store.is_active ? 'Отключить' : 'Включить'}
                    </button>
                  )}
                  {canEdit && (
                    <button
                      className="link danger"
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await api.deleteStore(store.id);
                          onReload();
                        }, `${store.name} удалён`)
                      }
                    >
                      удалить
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Код кабинета не меняется: по нему в переменной окружения WB_TOKENS ищется ключ API,
        и смена кода означала бы загрузку из другого кабинета при том же наборе товаров.
        Сам ключ в базе не хранится. Кабинет с товарами не удаляется — его отключают,
        и тогда его позиции выпадают из расчёта, а продажи остаются.
      </p>
    </>
  );
}

// ----------------------------------------------------------------------- поставщики

interface SuppliersProps {
  suppliers: Supplier[];
  canEdit: boolean;
  busy: boolean;
  act: Act;
  onSuppliers: (suppliers: Supplier[]) => void;
  onReload: () => void;
}

function SuppliersSection({ suppliers, canEdit, busy, act, onSuppliers, onReload }: SuppliersProps) {
  const [name, setName] = useState('');
  const [leadTime, setLeadTime] = useState('14');

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const ok = await act(async () => {
      await api.createSupplier({
        name: name.trim(),
        lead_time_days: leadTime ? Number(leadTime) : 14,
      });
      onReload();
    }, `Поставщик «${name.trim()}» заведён`);
    if (ok) setName('');
  };

  const patch = (updated: Supplier) =>
    onSuppliers(suppliers.map((row) => (row.id === updated.id ? updated : row)));

  return (
    <>
      {canEdit && (
        <form className="price-form" onSubmit={(event) => void create(event)}>
          <label className="inline-field grow">
            Поставщик
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="ООО «Поставка»" />
          </label>
          <label className="inline-field">
            Срок поставки, дней
            <input value={leadTime} onChange={(e) => setLeadTime(e.target.value)} />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            Завести
          </button>
        </form>
      )}

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Поставщик</th>
              <th>Срок поставки</th>
              <th>MOQ</th>
              <th>Товаров</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {suppliers.map((supplier) => (
              <tr key={supplier.id}>
                <td>
                  {canEdit ? (
                    <InlineText
                      value={supplier.name}
                      disabled={busy}
                      onCommit={(next) =>
                        act(async () => patch(await api.updateSupplier(supplier.id, { name: next })), 'Название изменено', false)
                      }
                    />
                  ) : (
                    supplier.name
                  )}
                </td>
                <td className="num">
                  {canEdit ? (
                    <InlineNumber
                      value={supplier.lead_time_days}
                      disabled={busy}
                      onCommit={(next) =>
                        act(
                          async () => patch(await api.updateSupplier(supplier.id, { lead_time_days: next })),
                          `Срок поставки — ${next} дн.: дата заказа по его товарам пересчитана`,
                        )
                      }
                    />
                  ) : (
                    supplier.lead_time_days
                  )}
                </td>
                <td className="num">
                  {canEdit ? (
                    <InlineNumber
                      value={supplier.moq}
                      disabled={busy}
                      onCommit={(next) =>
                        act(
                          async () => patch(await api.updateSupplier(supplier.id, { moq: next })),
                          `MOQ — ${next} шт.`,
                        )
                      }
                    />
                  ) : (
                    supplier.moq
                  )}
                </td>
                <td className="num">{supplier.product_count}</td>
                <td>
                  {canEdit && supplier.product_count === 0 && (
                    <button
                      className="link danger"
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await api.deleteSupplier(supplier.id);
                          onReload();
                        }, `«${supplier.name}» удалён`, false)
                      }
                    >
                      удалить
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Срок поставки и MOQ поставщика — то, что берётся, когда у товара пусто: правка
        сразу меняет дату и объём заказа по всем его товарам. Поставщик, на котором есть
        товары или заказы, не удаляется: заказы подписаны им, и запись должна их пережить.
      </p>
    </>
  );
}

// ------------------------------------------------------------------------ категории

interface CategoriesProps {
  categories: Category[];
  canEdit: boolean;
  busy: boolean;
  act: Act;
  onCategories: (categories: Category[]) => void;
  onReload: () => void;
}

function CategoriesSection({
  categories,
  canEdit,
  busy,
  act,
  onCategories,
  onReload,
}: CategoriesProps) {
  const [name, setName] = useState('');
  const [niche, setNiche] = useState('');

  const create = async (event: React.FormEvent) => {
    event.preventDefault();
    const ok = await act(async () => {
      await api.createCategory({ name: name.trim(), niche: niche.trim() || null });
      onReload();
    }, `Категория «${name.trim()}» заведена. Коэффициенты сезонности задаются на своём экране.`);
    if (ok) {
      setName('');
      setNiche('');
    }
  };

  const patch = (updated: Category) =>
    onCategories(categories.map((row) => (row.id === updated.id ? updated : row)));

  return (
    <>
      {canEdit && (
        <form className="price-form" onSubmit={(event) => void create(event)}>
          <label className="inline-field grow">
            Категория
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Лампы" />
          </label>
          <label className="inline-field">
            Ниша
            <input value={niche} onChange={(e) => setNiche(e.target.value)} placeholder="Автотовары" />
          </label>
          <button className="primary" type="submit" disabled={busy}>
            Завести
          </button>
        </form>
      )}

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Категория</th>
              <th>Ниша</th>
              <th>ABC</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {categories.map((category) => (
              <tr key={category.id}>
                <td>
                  {canEdit ? (
                    <InlineText
                      value={category.name}
                      disabled={busy}
                      onCommit={(next) =>
                        act(async () => patch(await api.updateCategory(category.id, { name: next })), 'Название изменено')
                      }
                    />
                  ) : (
                    category.name
                  )}
                </td>
                <td>
                  {canEdit ? (
                    <InlineText
                      value={category.niche ?? ''}
                      allowEmpty
                      placeholder="без ниши"
                      disabled={busy}
                      onCommit={(next) =>
                        act(
                          async () => patch(await api.updateCategory(category.id, { niche: next || null })),
                          'Ниша изменена',
                        )
                      }
                    />
                  ) : (
                    (category.niche ?? '—')
                  )}
                </td>
                <td>{category.abc_class ?? '—'}</td>
                <td>
                  {canEdit && (
                    <button
                      className="link danger"
                      disabled={busy}
                      onClick={() =>
                        void act(async () => {
                          await api.deleteCategory(category.id);
                          onReload();
                        }, `«${category.name}» удалена`)
                      }
                    >
                      удалить
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Категория — то, от чего товар берёт недельные коэффициенты сезонности, поэтому
        смена категории меняет его прогноз. Категорию с товарами или с заданной сезонностью
        не удалить: сначала перенесите товары.
      </p>
    </>
  );
}

// --------------------------------------------------------------- правка прямо в строке

interface InlineTextProps {
  value: string;
  disabled: boolean;
  placeholder?: string;
  /** Пустое значение допустимо: так снимают номер номенклатуры или нишу. */
  allowEmpty?: boolean;
  /** Возвращает `false`, если сервер отказал: тогда в поле возвращается прежнее. */
  onCommit: (value: string) => Promise<boolean>;
}

function InlineText({ value, disabled, placeholder, allowEmpty, onCommit }: InlineTextProps) {
  const [draft, setDraft] = useState(value);

  useEffect(() => {
    setDraft(value);
  }, [value]);

  const commit = async () => {
    const next = draft.trim();
    if (next === value.trim()) return;
    if (!next && !allowEmpty) {
      setDraft(value);
      return;
    }
    if (!(await onCommit(next))) setDraft(value);
  };

  return (
    <input
      className="name-edit"
      value={draft}
      disabled={disabled}
      placeholder={placeholder}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') setDraft(value);
      }}
    />
  );
}

interface InlineNumberProps {
  value: number;
  disabled: boolean;
  onCommit: (value: number) => Promise<boolean>;
}

function InlineNumber({ value, disabled, onCommit }: InlineNumberProps) {
  const [draft, setDraft] = useState(String(value));

  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  const commit = async () => {
    const next = Number(draft.replace(',', '.'));
    if (!Number.isFinite(next) || next < 0) {
      setDraft(String(value));
      return;
    }
    if (next === value) return;
    if (!(await onCommit(next))) setDraft(String(value));
  };

  return (
    <input
      className="name-edit num-edit"
      value={draft}
      disabled={disabled}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') setDraft(String(value));
      }}
    />
  );
}
