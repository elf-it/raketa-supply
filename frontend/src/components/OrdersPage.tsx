import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../api';
import type { Order, OrderStatus } from '../types';

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const moneyFormat = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
});

function formatMoment(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

const FILTERS: { id: OrderStatus | 'all'; label: string }[] = [
  { id: 'all', label: 'Все' },
  { id: 'draft', label: 'Черновики' },
  { id: 'placed', label: 'Размещённые' },
  { id: 'received', label: 'Принятые' },
  { id: 'cancelled', label: 'Отменённые' },
];

/** Подпись действия — то, что делает человек, а не название статуса. */
const ACTIONS: Record<OrderStatus, string> = {
  draft: 'Вернуть в черновик',
  placed: 'Разместить',
  received: 'Принять',
  cancelled: 'Отменить',
};

interface InlineEditProps {
  value: string;
  kind: 'qty' | 'week' | 'note';
  disabled?: boolean;
  placeholder?: string;
  /** Пустое значение: для примечания это «нет примечания», для чисел — отказ. */
  allowEmpty?: boolean;
  /** Возвращает `false`, если сервер правку не принял: поле вернёт прежнее значение. */
  onCommit: (value: string) => Promise<boolean>;
}

/**
 * Поле правки черновика. Enter и потеря фокуса отправляют значение, Escape
 * возвращает прежнее. Значение приходит сверху: после ответа сервера экран
 * перечитывает заказы, и поле показывает то, что действительно сохранено.
 * Отвергнутое значение в поле не остаётся — иначе экран показывал бы число,
 * которого в системе нет, а отказ ушёл бы из виду вместе с сообщением.
 */
function InlineEdit({
  value,
  kind,
  disabled,
  placeholder,
  allowEmpty,
  onCommit,
}: InlineEditProps) {
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
    // Мусор в количестве поле не принимает само — как ячейка прогноза в матрице.
    // Неделю проверяет сервер: только он знает, какие недели уже закрыты.
    if (kind === 'qty') {
      const qty = Number(next.replace(',', '.'));
      if (!Number.isInteger(qty) || qty <= 0) {
        setDraft(value);
        return;
      }
    }
    if (!(await onCommit(next))) setDraft(value);
  };

  return (
    <span className={`editable ${kind}`}>
      <input
        value={draft}
        disabled={disabled}
        placeholder={placeholder}
        inputMode={kind === 'qty' ? 'numeric' : 'text'}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur();
          if (event.key === 'Escape') {
            setDraft(value);
            event.currentTarget.blur();
          }
        }}
      />
    </span>
  );
}

interface Props {
  canEdit: boolean;
  onError: (message: string | null) => void;
  /** Заказ меняет товары в пути, поэтому матрицу и закупку нужно перечитать. */
  onChanged: () => void;
}

export function OrdersPage({ canEdit, onError, onChanged }: Props) {
  const [orders, setOrders] = useState<Order[]>([]);
  const [filter, setFilter] = useState<OrderStatus | 'all'>('all');
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      setOrders(await api.orders(filter === 'all' ? undefined : filter));
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [filter, onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const totals = useMemo(
    () =>
      orders.reduce(
        (acc, order) => {
          if (order.status === 'placed') {
            acc.placed += 1;
            acc.qty += order.total_qty;
            acc.cost += order.total_cost;
          }
          return acc;
        },
        { placed: 0, qty: 0, cost: 0 },
      ),
    [orders],
  );

  /** Правится только черновик и только тем, кому разрешено менять данные. */
  const editable = (order: Order) => canEdit && order.status === 'draft';

  const move = async (order: Order, status: OrderStatus) => {
    setBusy(order.id);
    try {
      await api.setOrderStatus(order.id, status);
      await load();
      onChanged();
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  /**
   * Правка черновика. Список перечитывается целиком: правка строки меняет
   * итоги заказа, а они стоят в свёрнутой строке над составом.
   */
  const edit = async (order: Order, action: () => Promise<unknown>): Promise<boolean> => {
    setBusy(order.id);
    let failure: string | null = null;
    try {
      await action();
    } catch (err) {
      failure = (err as Error).message;
    }
    // Сначала перечитываем (он сам снимает прошлую ошибку), потом показываем свою:
    // иначе отказ сервера стёрся бы успешной загрузкой списка.
    await load();
    if (failure) onError(failure);
    setBusy(null);
    return failure === null;
  };

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Заказы поставщикам</h2>
          <p className="muted">
            Размещённый заказ становится товаром в пути: закупочная потребность уменьшается
            на него сама, отменённый — возвращает её обратно.
          </p>
        </div>
        <div className="panel-totals">
          <span>
            размещено: <strong>{totals.placed}</strong>
          </span>
          <span>
            <strong>{numberFormat.format(totals.qty)}</strong> шт. в размещённых
          </span>
          <span>
            <strong>{moneyFormat.format(totals.cost)}</strong>
          </span>
        </div>
      </header>

      <div className="purchase-controls">
        <div className="switch">
          {FILTERS.map((item) => (
            <button
              key={item.id}
              className={filter === item.id ? 'active' : ''}
              onClick={() => setFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Заказ</th>
              <th>Поставщик</th>
              <th>Статус</th>
              <th>Позиций</th>
              <th>Количество</th>
              <th>Сумма</th>
              <th>м³</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>
            {orders.map((order) => (
              <Fragment key={order.id}>
                <tr className={order.status === 'cancelled' ? 'idle-row' : ''}>
                  <td>
                    <div className="row-title">№ {order.id}</div>
                    <div className="row-subtitle">
                      {formatMoment(order.created_at)}
                      {order.created_by ? ` · ${order.created_by}` : ''}
                    </div>
                    <button className="link small" onClick={() => toggle(order.id)}>
                      {expanded.has(order.id) ? 'свернуть состав' : 'состав заказа'}
                    </button>
                  </td>
                  <td>{order.supplier ?? '—'}</td>
                  <td>
                    <span className={`badge order ${order.status}`}>{order.status_label}</span>
                    {order.placed_at && (
                      <div className="row-subtitle">размещён {formatMoment(order.placed_at)}</div>
                    )}
                  </td>
                  <td className="num">{order.lines_count}</td>
                  <td className="num strong">{numberFormat.format(order.total_qty)}</td>
                  <td className="num">{moneyFormat.format(order.total_cost)}</td>
                  <td className="num">{order.total_volume_m3.toFixed(3)}</td>
                  <td>
                    <div className="order-actions">
                      {canEdit &&
                        order.next_statuses.map((next) => (
                          <button
                            key={next}
                            className={next === 'cancelled' ? 'ghost small' : 'small'}
                            disabled={busy === order.id}
                            onClick={() => void move(order, next)}
                          >
                            {ACTIONS[next]}
                          </button>
                        ))}
                      {/* Обычная ссылка, а не fetch: браузер сам сохранит файл
                          с именем, которое прислал сервер. */}
                      <a className="button-link small" href={`/api/v1/orders/${order.id}/export`}>
                        Скачать .xlsx
                      </a>
                    </div>
                  </td>
                </tr>
                {expanded.has(order.id) && (
                  <tr className="detail-row">
                    <td colSpan={8}>
                      <table className="purchase inner">
                        <thead>
                          <tr>
                            <th>Товар</th>
                            <th>Магазин</th>
                            <th>Приход</th>
                            <th>Количество</th>
                            <th>Сумма</th>
                            {editable(order) && <th>Строка</th>}
                          </tr>
                        </thead>
                        <tbody>
                          {order.lines.map((line) => (
                            <tr key={line.id}>
                              <td>
                                <div className="row-title">{line.title}</div>
                                <div className="row-subtitle">арт. {line.vendor_code}</div>
                              </td>
                              <td>{line.store_name}</td>
                              <td>
                                {editable(order) ? (
                                  <InlineEdit
                                    value={line.arrival_week}
                                    kind="week"
                                    disabled={busy === order.id}
                                    onCommit={(week) =>
                                      edit(order, () =>
                                        api.updateOrderLine(order.id, line.id, {
                                          arrival_week: week,
                                        }),
                                      )
                                    }
                                  />
                                ) : (
                                  line.arrival_week
                                )}
                              </td>
                              <td className="num strong">
                                {editable(order) ? (
                                  <InlineEdit
                                    value={String(line.qty)}
                                    kind="qty"
                                    disabled={busy === order.id}
                                    onCommit={(qty) =>
                                      edit(order, () =>
                                        api.updateOrderLine(order.id, line.id, {
                                          qty: Number(qty),
                                        }),
                                      )
                                    }
                                  />
                                ) : (
                                  numberFormat.format(line.qty)
                                )}
                              </td>
                              <td className="num">{moneyFormat.format(line.cost)}</td>
                              {editable(order) && (
                                <td>
                                  <button
                                    className="ghost small"
                                    disabled={busy === order.id || order.lines.length === 1}
                                    title={
                                      order.lines.length === 1
                                        ? 'Последняя строка держит заказ: его отменяют целиком'
                                        : 'Убрать позицию из черновика'
                                    }
                                    onClick={() =>
                                      void edit(order, () =>
                                        api.deleteOrderLine(order.id, line.id),
                                      )
                                    }
                                  >
                                    Убрать
                                  </button>
                                </td>
                              )}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {editable(order) ? (
                        <p className="hint note-edit">
                          Примечание поставщику:{' '}
                          <InlineEdit
                            value={order.note ?? ''}
                            kind="note"
                            allowEmpty
                            disabled={busy === order.id}
                            placeholder="например, отгрузить одной машиной"
                            onCommit={(note) =>
                              edit(order, () => api.setOrderNote(order.id, note || null))
                            }
                          />
                        </p>
                      ) : (
                        order.note && <p className="hint">Примечание: {order.note}</p>
                      )}
                      <p className="hint">
                        {editable(order)
                          ? 'Количество, неделю прихода и примечание правят до размещения: ' +
                            'размещённый заказ уже ушёл поставщику и считается товаром в пути. ' +
                            'Себестоимость записана при создании заказа, правка её не меняет.'
                          : 'Себестоимость записана на момент создания заказа: сумма не меняется ' +
                            'задним числом вслед за карточкой товара. Правится только черновик — ' +
                            'размещённый заказ отменяют и собирают заново.'}
                      </p>
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
            {orders.length === 0 && (
              <tr>
                <td className="empty" colSpan={8}>
                  Заказов нет. Черновик собирается на экране закупки кнопкой у поставщика.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
