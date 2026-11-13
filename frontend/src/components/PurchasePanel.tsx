import type { PurchasePlan } from '../types';

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });
const moneyFormat = new Intl.NumberFormat('ru-RU', {
  style: 'currency',
  currency: 'RUB',
  maximumFractionDigits: 0,
});

function formatDate(value: string | null): string {
  if (!value) return '—';
  const [year, month, day] = value.split('-');
  return `${day}.${month}.${year}`;
}

export function PurchasePanel({ plan }: { plan: PurchasePlan }) {
  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Закупочная потребность</h2>
          <p className="muted">
            Считается по исходному спросу с учётом остатков, партий в пути, lead time, MOQ, PCB,
            coverage и safety.
          </p>
        </div>
        <div className="panel-totals">
          <span>
            <strong>{numberFormat.format(plan.total_qty)}</strong> шт.
          </span>
          <span>
            <strong>{moneyFormat.format(plan.total_cost)}</strong>
          </span>
          <span>
            <strong>{plan.total_volume_m3.toFixed(2)}</strong> м³
          </span>
          {plan.overdue_count > 0 && (
            <span className="overdue">просрочено заказов: {plan.overdue_count}</span>
          )}
        </div>
      </header>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Товар</th>
              <th>Магазин</th>
              <th>Остаток</th>
              <th>Дефицит с</th>
              <th>Заказать до</th>
              <th>Количество</th>
              <th>Стоимость</th>
              <th>м³</th>
              <th>Приход</th>
            </tr>
          </thead>
          <tbody>
            {plan.rows.map((row) => (
              <tr key={row.pair_id} className={row.is_overdue ? 'overdue-row' : undefined}>
                <td>
                  <div className="row-title">{row.title}</div>
                  <div className="row-subtitle">арт. {row.vendor_code}</div>
                </td>
                <td>{row.store_name}</td>
                <td className="num">{numberFormat.format(Math.round(row.current_stock))}</td>
                <td>{row.shortage_week ?? '—'}</td>
                <td className={row.is_overdue ? 'num danger' : 'num'}>
                  {formatDate(row.order_date)}
                </td>
                <td className="num strong">{numberFormat.format(row.qty)}</td>
                <td className="num">{moneyFormat.format(row.cost)}</td>
                <td className="num">{row.volume_m3.toFixed(3)}</td>
                <td>{row.arrival_week ?? '—'}</td>
              </tr>
            ))}
            {plan.rows.length === 0 && (
              <tr>
                <td className="empty" colSpan={9}>
                  Заказывать нечего: остатков и партий в пути хватает на весь горизонт
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
