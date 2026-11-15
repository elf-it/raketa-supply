import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../api';
import type { PriceCard, PriceRecord, Product } from '../types';

const money = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });

function rub(value: number): string {
  return money.format(value);
}

/** `2026-11-15` → `15.11.2026`. Через `Date` дата-без-времени уехала бы на часовой пояс. */
function day(iso: string | null): string {
  if (!iso) return '—';
  const [year, month, date] = iso.split('-');
  return `${date}.${month}.${year}`;
}

function moment(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function today(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, '0');
  const date = `${now.getDate()}`.padStart(2, '0');
  return `${now.getFullYear()}-${month}-${date}`;
}

/** Наценка к себестоимости — то, из чего живёт позиция. Без цены считать нечего. */
function markup(cost: number, price: number): number | null {
  if (cost <= 0 || price <= 0) return null;
  return (price / cost - 1) * 100;
}

function percent(value: number | null): string {
  if (value === null) return '—';
  return `${value > 0 ? '+' : value < 0 ? '−' : ''}${Math.abs(value).toFixed(0)}%`;
}

interface Props {
  onError: (message: string | null) => void;
  canEdit: boolean;
}

interface FormState {
  valid_from: string;
  cost_price: string;
  price: string;
  note: string;
}

const EMPTY_FORM: FormState = { valid_from: '', cost_price: '', price: '', note: '' };

/**
 * История цен и себестоимости (задача B-9).
 *
 * В строке товара — то, что действует сейчас, и дата, с которой действует:
 * без даты непонятно, вчерашняя эта цена или прошлогодняя. Щелчок по строке
 * раскрывает всю череду изменений и форму новой записи.
 *
 * Поиск здесь местный, а не запросом на сервер: товаров столько же, сколько
 * позиций в каталоге, они уже загружены, и отбор по подстроке в браузере
 * отвечает мгновенно — в отличие от матрицы, где за фильтром стоит расчёт.
 */
export function PricesPage({ onError, canEdit }: Props) {
  const [products, setProducts] = useState<Product[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [search, setSearch] = useState('');
  const [openId, setOpenId] = useState<number | null>(null);
  const [card, setCard] = useState<PriceCard | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    try {
      setProducts(await api.products());
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [onError]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const shown = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) return products;
    return products.filter((product) =>
      [product.vendor_code, product.title, product.supplier ?? '']
        .join(' ')
        .toLowerCase()
        .includes(needle),
    );
  }, [products, search]);

  /** Карточка в ответе — последнее слово сервера: ею обновляется и строка списка. */
  const absorb = (answer: PriceCard) => {
    setCard(answer);
    setProducts((prev) =>
      prev.map((product) =>
        product.id === answer.product_id
          ? {
              ...product,
              cost_price: answer.cost_price,
              price: answer.price,
              price_valid_from: answer.valid_from,
            }
          : product,
      ),
    );
  };

  const open = async (product: Product) => {
    if (openId === product.id) {
      setOpenId(null);
      setCard(null);
      return;
    }
    setOpenId(product.id);
    setCard(null);
    setForm({ ...EMPTY_FORM, valid_from: today() });
    setBusy(true);
    try {
      setCard(await api.prices(product.id));
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const save = async (productId: number) => {
    const cost = form.cost_price.trim();
    const price = form.price.trim();
    if (!cost && !price) {
      onError('Чтобы записать цену, заполните себестоимость или цену продажи');
      return;
    }
    setBusy(true);
    try {
      absorb(
        await api.addPrice(productId, {
          valid_from: form.valid_from,
          ...(cost ? { cost_price: Number(cost) } : {}),
          ...(price ? { price: Number(price) } : {}),
          ...(form.note.trim() ? { note: form.note.trim() } : {}),
        }),
      );
      setForm({ ...EMPTY_FORM, valid_from: today() });
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const drop = async (productId: number, record: PriceRecord) => {
    setBusy(true);
    try {
      absorb(await api.deletePrice(productId, record.id));
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const change = (record: PriceRecord, older: PriceRecord | undefined): string => {
    if (!older || older.cost_price <= 0) return '—';
    return percent((record.cost_price / older.cost_price - 1) * 100);
  };

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Цены и себестоимость</h2>
          <p className="muted">
            Себестоимость участвует в расчёте закупки, поэтому новая цена не стирает прежнюю:
            каждая записывается с даты, с которой действует. Сумма уже собранного заказа от
            этого не меняется — в его строках свой снимок цены.
          </p>
        </div>
        <label className="inline-field">
          Поиск
          <input
            type="search"
            value={search}
            placeholder="артикул, название, поставщик"
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      </header>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Товар</th>
              <th>Поставщик</th>
              <th className="num">Себестоимость</th>
              <th className="num">Цена</th>
              <th className="num">Наценка</th>
              <th>Действует с</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {shown.map((product) => {
              const isOpen = openId === product.id;
              return (
                <Fragment key={product.id}>
                <tr>
                  <td>
                    <button className="link row-title" onClick={() => void open(product)}>
                      {product.title}
                    </button>
                    <div className="row-subtitle">арт. {product.vendor_code}</div>
                  </td>
                  <td>{product.supplier ?? '—'}</td>
                  <td className="num strong">{rub(product.cost_price)}</td>
                  <td className="num">{rub(product.price)}</td>
                  <td className="num">{percent(markup(product.cost_price, product.price))}</td>
                  <td>
                    {product.price_valid_from ? (
                      day(product.price_valid_from)
                    ) : (
                      <span className="muted" title="Товар достался системе с ценой, но без даты">
                        неизвестно
                      </span>
                    )}
                  </td>
                  <td>
                    <button className="link" onClick={() => void open(product)}>
                      {isOpen ? 'Скрыть' : 'История'}
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr className="detail-row">
                    <td colSpan={7}>
                      {card === null ? (
                        <span className="spinner">загружаю историю…</span>
                      ) : (
                        <div className="price-history">
                          {card.history.length === 0 ? (
                            <p className="hint">
                              Истории нет: цена досталась системе из начальной загрузки, и с какой
                              даты она действует, неизвестно. Первая запись появится здесь.
                            </p>
                          ) : (
                            <table className="purchase inner">
                              <thead>
                                <tr>
                                  <th>Действует с</th>
                                  <th className="num">Себестоимость</th>
                                  <th className="num">Изменение</th>
                                  <th className="num">Цена</th>
                                  <th>Кто и когда записал</th>
                                  <th>Примечание</th>
                                  {canEdit && <th />}
                                </tr>
                              </thead>
                              <tbody>
                                {card.history.map((record, index) => (
                                  <tr
                                    key={record.id}
                                    className={index === 0 ? 'current-price' : undefined}
                                  >
                                    <td>
                                      {day(record.valid_from)}
                                      {index === 0 && (
                                        <div className="row-subtitle">действует</div>
                                      )}
                                    </td>
                                    <td className="num strong">{rub(record.cost_price)}</td>
                                    <td className="num">
                                      {change(record, card.history[index + 1])}
                                    </td>
                                    <td className="num">{rub(record.price)}</td>
                                    <td>
                                      {record.author ?? '—'}
                                      <div className="row-subtitle">{moment(record.created_at)}</div>
                                    </td>
                                    <td className="muted">{record.note ?? '—'}</td>
                                    {canEdit && (
                                      <td>
                                        <button
                                          className="link danger"
                                          disabled={busy}
                                          title="Для ошибочной записи: дата или сумма введены неверно"
                                          onClick={() => void drop(product.id, record)}
                                        >
                                          убрать
                                        </button>
                                      </td>
                                    )}
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          )}

                          {canEdit && (
                            <div className="price-form">
                              <label className="inline-field">
                                С даты
                                <input
                                  type="date"
                                  max={today()}
                                  value={form.valid_from}
                                  onChange={(event) =>
                                    setForm({ ...form, valid_from: event.target.value })
                                  }
                                />
                              </label>
                              <label className="inline-field">
                                Себестоимость
                                <input
                                  type="number"
                                  min={0}
                                  step="0.01"
                                  placeholder={rub(card.cost_price)}
                                  value={form.cost_price}
                                  onChange={(event) =>
                                    setForm({ ...form, cost_price: event.target.value })
                                  }
                                />
                              </label>
                              <label className="inline-field">
                                Цена
                                <input
                                  type="number"
                                  min={0}
                                  step="0.01"
                                  placeholder={rub(card.price)}
                                  value={form.price}
                                  onChange={(event) =>
                                    setForm({ ...form, price: event.target.value })
                                  }
                                />
                              </label>
                              <label className="inline-field grow">
                                Примечание
                                <input
                                  type="text"
                                  maxLength={500}
                                  placeholder="счёт поставщика, новый прайс"
                                  value={form.note}
                                  onChange={(event) =>
                                    setForm({ ...form, note: event.target.value })
                                  }
                                />
                              </label>
                              <button
                                className="small"
                                disabled={busy || !form.valid_from}
                                onClick={() => void save(product.id)}
                              >
                                Записать
                              </button>
                            </div>
                          )}

                          <p className="footnote">
                            Пустое поле означает «оставить как было»: правка себестоимости не
                            обнуляет цену продажи. Дату будущего система не принимает — применить
                            такую цену в срок ей нечем, а считать заказы по цене, которой ещё нет,
                            хуже, чем ввести её первого числа.
                          </p>
                        </div>
                      )}
                    </td>
                  </tr>
                )}
                </Fragment>
              );
            })}
            {shown.length === 0 && (
              <tr>
                <td className="empty" colSpan={7}>
                  {loaded ? 'Под поиск ничего не попало' : 'Загружаю…'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
