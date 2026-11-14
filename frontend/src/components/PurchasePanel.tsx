import { useMemo, useState } from 'react';

import type { PurchasePlan, PurchaseRow } from '../types';

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

function qty(value: number): string {
  return numberFormat.format(Math.round(value));
}

const NO_SUPPLIER = 'Без поставщика';

/** Группа заказа: заказ уходит поставщику, а не отдельному артикулу. */
interface Group {
  supplier: string;
  rows: PurchaseRow[];
  qty: number;
  cost: number;
  volume: number;
  overdue: number;
  firstOrderDate: string | null;
}

function groupBySupplier(rows: PurchaseRow[]): Group[] {
  const map = new Map<string, Group>();
  for (const row of rows) {
    const supplier = row.supplier ?? NO_SUPPLIER;
    let group = map.get(supplier);
    if (!group) {
      group = {
        supplier,
        rows: [],
        qty: 0,
        cost: 0,
        volume: 0,
        overdue: 0,
        firstOrderDate: null,
      };
      map.set(supplier, group);
    }
    group.rows.push(row);
    group.qty += row.qty;
    group.cost += row.cost;
    group.volume += row.volume_m3;
    if (row.is_overdue && row.needed) group.overdue += 1;
    if (row.order_date && (!group.firstOrderDate || row.order_date < group.firstOrderDate)) {
      group.firstOrderDate = row.order_date;
    }
  }
  const groups = [...map.values()];
  for (const group of groups) {
    group.rows.sort((a, b) => (a.order_date ?? '9999').localeCompare(b.order_date ?? '9999'));
  }
  // Сначала то, что заказывать раньше: просроченное и ближайшее по сроку.
  groups.sort((a, b) =>
    (a.firstOrderDate ?? '9999').localeCompare(b.firstOrderDate ?? '9999'),
  );
  return groups;
}

/** Срочность по дате заказа, а не по дефициту: заказ размещают заранее. */
function urgency(row: PurchaseRow): { label: string; tone: string } | null {
  if (!row.needed || !row.order_date) return null;
  if (row.is_overdue) return { label: 'просрочено', tone: 'danger' };
  const days = Math.round(
    (new Date(`${row.order_date}T00:00:00`).getTime() - Date.now()) / 86_400_000,
  );
  if (days <= 7) return { label: 'на этой неделе', tone: 'warn' };
  return null;
}

interface Props {
  plan: PurchasePlan;
  onlyNeeded: boolean;
  onOnlyNeededChange: (value: boolean) => void;
}

export function PurchasePanel({ plan, onlyNeeded, onOnlyNeededChange }: Props) {
  const [grouped, setGrouped] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const groups = useMemo(() => groupBySupplier(plan.rows), [plan.rows]);
  const flat = useMemo(
    () =>
      [...plan.rows].sort((a, b) =>
        (a.order_date ?? '9999').localeCompare(b.order_date ?? '9999'),
      ),
    [plan.rows],
  );

  const toggleRow = (pairId: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(pairId)) next.delete(pairId);
      else next.add(pairId);
      return next;
    });

  const toggleGroup = (supplier: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(supplier)) next.delete(supplier);
      else next.add(supplier);
      return next;
    });

  const columns = 8;

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
            <strong>{qty(plan.total_qty)}</strong> шт.
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

      <div className="purchase-controls">
        <div className="switch">
          <button
            className={onlyNeeded ? 'active' : ''}
            onClick={() => onOnlyNeededChange(true)}
          >
            Только к заказу
          </button>
          <button
            className={onlyNeeded ? '' : 'active'}
            onClick={() => onOnlyNeededChange(false)}
          >
            Все позиции
          </button>
        </div>
        <div className="switch">
          <button className={grouped ? 'active' : ''} onClick={() => setGrouped(true)}>
            По поставщикам
          </button>
          <button className={grouped ? '' : 'active'} onClick={() => setGrouped(false)}>
            По сроку
          </button>
        </div>
        <span className="muted">
          {grouped
            ? 'Заказ уходит поставщику целиком — MOQ и партии считаются по позиции, итог по группе'
            : 'Все позиции подряд, ближайший срок заказа сверху'}
        </span>
      </div>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Товар</th>
              <th>Остаток</th>
              <th>Дефицит с</th>
              <th>Заказать до</th>
              <th>Количество</th>
              <th>Стоимость</th>
              <th>м³</th>
              <th>Приход</th>
            </tr>
          </thead>
          {grouped ? (
            groups.map((group) => (
              <tbody key={group.supplier}>
                <tr className="group-head">
                  <th colSpan={4}>
                    <button
                      className="group-toggle"
                      onClick={() => toggleGroup(group.supplier)}
                      aria-expanded={!collapsed.has(group.supplier)}
                    >
                      <span className="caret">
                        {collapsed.has(group.supplier) ? '▸' : '▾'}
                      </span>
                      {group.supplier}
                    </button>
                    <span className="group-meta">
                      {group.rows.length} поз. · заказ до {formatDate(group.firstOrderDate)}
                      {group.overdue > 0 && (
                        <span className="badge status short">просрочено {group.overdue}</span>
                      )}
                    </span>
                  </th>
                  <td className="num strong">{qty(group.qty)}</td>
                  <td className="num">{moneyFormat.format(group.cost)}</td>
                  <td className="num">{group.volume.toFixed(3)}</td>
                  <td />
                </tr>
                {!collapsed.has(group.supplier) &&
                  group.rows.map((row) => (
                    <PurchaseRows
                      key={row.pair_id}
                      row={row}
                      expanded={expanded.has(row.pair_id)}
                      onToggle={() => toggleRow(row.pair_id)}
                      columns={columns}
                    />
                  ))}
              </tbody>
            ))
          ) : (
            <tbody>
              {flat.map((row) => (
                <PurchaseRows
                  key={row.pair_id}
                  row={row}
                  expanded={expanded.has(row.pair_id)}
                  onToggle={() => toggleRow(row.pair_id)}
                  columns={columns}
                />
              ))}
            </tbody>
          )}
          {plan.rows.length === 0 && (
            <tbody>
              <tr>
                <td className="empty" colSpan={columns}>
                  {onlyNeeded
                    ? 'Заказывать нечего: остатков и партий в пути хватает на весь горизонт'
                    : 'Под фильтры не попало ни одной позиции'}
                </td>
              </tr>
            </tbody>
          )}
        </table>
      </div>
    </section>
  );
}

interface RowProps {
  row: PurchaseRow;
  expanded: boolean;
  onToggle: () => void;
  columns: number;
}

function PurchaseRows({ row, expanded, onToggle, columns }: RowProps) {
  const mark = urgency(row);
  const rounded = row.needed && row.qty !== Math.round(row.qty_before_constraints);

  return (
    <>
      <tr className={[row.is_overdue ? 'overdue-row' : '', row.needed ? '' : 'idle-row'].join(' ')}>
        <td>
          <div className="row-title">{row.title}</div>
          <div className="row-subtitle">
            арт. {row.vendor_code} · {row.store_name}
          </div>
          {/* Разбор живёт в первой колонке: правый край таблицы уезжает за экран. */}
          <button className="link small" onClick={onToggle} aria-expanded={expanded}>
            {expanded ? 'свернуть разбор' : 'разбор количества'}
          </button>
        </td>
        <td className="num">{qty(row.current_stock)}</td>
        <td>{row.shortage_week ?? '—'}</td>
        <td className={row.is_overdue ? 'num danger' : 'num'}>
          {formatDate(row.order_date)}
          {mark && <span className={`badge status ${mark.tone === 'danger' ? 'short' : 'over'}`}>{mark.label}</span>}
        </td>
        <td className="num strong">
          {row.needed ? qty(row.qty) : '—'}
          {rounded && (
            <span className="rounded-from">из {qty(row.qty_before_constraints)}</span>
          )}
        </td>
        <td className="num">{row.needed ? moneyFormat.format(row.cost) : '—'}</td>
        <td className="num">{row.needed ? row.volume_m3.toFixed(3) : '—'}</td>
        <td>{row.arrival_week ?? '—'}</td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={columns}>
            <Explanation row={row} />
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * Объяснение количества. Все числа приходят с бэкенда из того же расчёта —
 * формула здесь не повторяется, иначе экран и ядро однажды разойдутся.
 */
function Explanation({ row }: { row: PurchaseRow }) {
  if (!row.needed) {
    return (
      <div className="explain">
        <p className="muted">{row.reason}</p>
        <Params row={row} />
      </div>
    );
  }

  return (
    <div className="explain">
      <p>
        Остаток опускается ниже страхового запаса на неделе <b>{row.shortage_week}</b>. Окно
        покрытия — <b>{row.coverage_from}</b>…<b>{row.coverage_to}</b> ({row.coverage_weeks_used}{' '}
        нед. из {row.params.coverage_weeks} заданных).
      </p>
      <p className="formula">
        спрос окна <b>{qty(row.window_demand)}</b>
        {' + '}страховой запас <b>{qty(row.safety_stock)}</b>
        {' − '}остаток к началу окна <b>{qty(row.stock_before)}</b>
        {' − '}в пути <b>{qty(row.window_transit)}</b>
        {' = '}
        <b>{qty(row.qty_before_constraints)}</b>
        {row.qty !== Math.round(row.qty_before_constraints) && (
          <>
            {' → '}ограничения: кратность {row.params.pack_size}
            {row.params.moq > 0 ? `, MOQ ${row.params.moq}` : ''} → <b>{qty(row.qty)}</b>
          </>
        )}
      </p>
      <Params row={row} />
    </div>
  );
}

function Params({ row }: { row: PurchaseRow }) {
  const p = row.params;
  return (
    <dl className="params">
      <div>
        <dt>Поставщик</dt>
        <dd>{row.supplier ?? '—'}</dd>
      </div>
      <div>
        <dt>Категория</dt>
        <dd>{row.category ?? '—'}</dd>
      </div>
      <div>
        <dt>Срок поставки</dt>
        <dd>{p.lead_time_days} дн.</dd>
      </div>
      <div>
        <dt>MOQ</dt>
        <dd>{p.moq || '—'}</dd>
      </div>
      <div>
        <dt>Кратность</dt>
        <dd>{p.pack_size}</dd>
      </div>
      <div>
        <dt>Покрытие</dt>
        <dd>{p.coverage_weeks} нед.</dd>
      </div>
      <div>
        <dt>Страховой запас</dt>
        <dd>{p.safety_weeks} нед.</dd>
      </div>
      <div>
        <dt>Себестоимость</dt>
        <dd>{moneyFormat.format(p.cost_price)}</dd>
      </div>
      <div>
        <dt>Объём</dt>
        <dd>{p.volume_m3.toFixed(4)} м³</dd>
      </div>
    </dl>
  );
}
