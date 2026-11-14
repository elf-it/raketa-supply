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
                              <td>{line.arrival_week}</td>
                              <td className="num strong">{numberFormat.format(line.qty)}</td>
                              <td className="num">{moneyFormat.format(line.cost)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      <p className="hint">
                        Себестоимость записана на момент создания заказа: сумма не меняется
                        задним числом вслед за карточкой товара.
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
