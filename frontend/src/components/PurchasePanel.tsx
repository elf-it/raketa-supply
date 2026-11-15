import { useMemo, useState } from 'react';

import type { Order, PurchasePlan, PurchaseRow } from '../types';

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
  /** Готовая ссылка на выгрузку: фильтры и отбор знает приложение, не панель. */
  exportUrl: string;
  onlyNeeded: boolean;
  onOnlyNeededChange: (value: boolean) => void;
  canEdit: boolean;
  /** Собирает черновик заказа из строк группы. */
  onCreateOrder: (rows: PurchaseRow[]) => void;
  /** Незакрытые черновики: в них дописывается позиция, забытая при сборке. */
  drafts: Order[];
  onAddToDraft: (orderId: number, row: PurchaseRow) => void;
  /** Запрос по заказу в полёте: кнопки заказа на время ответа заперты. */
  orderBusy: boolean;
}

export function PurchasePanel({
  plan,
  exportUrl,
  onlyNeeded,
  onOnlyNeededChange,
  canEdit,
  onCreateOrder,
  drafts,
  onAddToDraft,
  orderBusy,
}: Props) {
  const [grouped, setGrouped] = useState(true);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());

  const groups = useMemo(() => groupBySupplier(plan.rows), [plan.rows]);
  // Черновики раскладываются по поставщику: заказ уходит поставщику целиком,
  // поэтому позиции предлагаются только его черновики. Сопоставление по
  // идентификатору, а не по названию — тёзки в справочнике не помеха.
  const draftsBySupplier = useMemo(() => {
    const map = new Map<number | null, Order[]>();
    for (const order of [...drafts].sort((a, b) => b.id - a.id)) {
      const list = map.get(order.supplier_id);
      if (list) list.push(order);
      else map.set(order.supplier_id, [order]);
    }
    return map;
  }, [drafts]);
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

  const columns = 9;

  /** Общие для обоих режимов свойства строки: группировка их не меняет. */
  const rowProps = (row: PurchaseRow) => ({
    row,
    expanded: expanded.has(row.pair_id),
    onToggle: () => toggleRow(row.pair_id),
    columns,
    drafts: canEdit ? (draftsBySupplier.get(row.supplier_id) ?? []) : [],
    onAddToDraft,
    orderBusy,
  });

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
        <a
          className="button-link small export-link"
          href={exportUrl}
          title="Весь разбор количества колонками: спрос окна, страховой запас, остаток, в пути"
        >
          Скачать .xlsx
        </a>
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
              <th title="Годовая доходность вложенных в заказ денег по валовой марже. Комиссия маркетплейса, логистика и хранение не вычтены: число годится для сравнения позиций между собой, а не как обещание доходности">
                ROI, % год.
              </th>
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
                    {canEdit && group.rows.some((row) => row.needed) && (
                      <button
                        className="small group-order"
                        disabled={orderBusy}
                        onClick={() => onCreateOrder(group.rows.filter((row) => row.needed))}
                      >
                        Сформировать заказ
                      </button>
                    )}
                  </th>
                  <td className="num strong">{qty(group.qty)}</td>
                  <td className="num">{moneyFormat.format(group.cost)}</td>
                  {/* По группе ROI не складывается: это доходность на вложенный
                      рубль, и сумма процентов смысла не имеет. */}
                  <td />
                  <td className="num">{group.volume.toFixed(3)}</td>
                  <td />
                </tr>
                {!collapsed.has(group.supplier) &&
                  group.rows.map((row) => <PurchaseRows key={row.pair_id} {...rowProps(row)} />)}
              </tbody>
            ))
          ) : (
            <tbody>
              {flat.map((row) => (
                <PurchaseRows key={row.pair_id} {...rowProps(row)} />
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
  /** Черновики этого же поставщика; пусто — добавлять некуда или нельзя. */
  drafts: Order[];
  onAddToDraft: (orderId: number, row: PurchaseRow) => void;
  orderBusy: boolean;
}

function PurchaseRows({
  row,
  expanded,
  onToggle,
  columns,
  drafts,
  onAddToDraft,
  orderBusy,
}: RowProps) {
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
          {row.needed && row.arrival_week && drafts.length > 0 && (
            <AddToDraft row={row} drafts={drafts} busy={orderBusy} onAdd={onAddToDraft} />
          )}
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
        <td className="num">
          {/* Пусто — не ноль: считать не из чего, если нет цены, себестоимости
              или спроса. Рядом стоит срок распродажи: по одному проценту
              решение не принимают. */}
          {row.roi_percent != null ? `${Math.round(row.roi_percent)} %` : '—'}
          {row.sell_out_weeks != null && (
            <span className="rounded-from">за {row.sell_out_weeks.toFixed(1)} нед.</span>
          )}
        </td>
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

interface AddProps {
  row: PurchaseRow;
  /** Только черновики того же поставщика, новые сверху. */
  drafts: Order[];
  busy: boolean;
  onAdd: (orderId: number, row: PurchaseRow) => void;
}

/**
 * Дописать позицию в черновик.
 *
 * Черновики, в которых эта позиция уже стоит, предлагать нечего: второй строки
 * той же позиции сервер не примет, и правильный ответ на «хочу больше» —
 * поправить количество в самом заказе. Поэтому такие черновики здесь только
 * названы: иначе человек не понял бы, куда делась кнопка.
 */
function AddToDraft({ row, drafts, busy, onAdd }: AddProps) {
  const [picked, setPicked] = useState<number | null>(null);

  const has = (order: Order) => order.lines.some((line) => line.pair_id === row.pair_id);
  const already = drafts.filter(has);
  const open = drafts.filter((order) => !has(order));

  if (open.length === 0) {
    return (
      <span className="draft-note">
        уже в черновике {already.map((order) => `№${order.id}`).join(', ')}
      </span>
    );
  }

  const target = open.find((order) => order.id === picked) ?? open[0];
  return (
    <span className="add-to-draft">
      {open.length > 1 && (
        <select
          value={target.id}
          disabled={busy}
          aria-label="Черновик, в который добавить позицию"
          onChange={(event) => setPicked(Number(event.target.value))}
        >
          {open.map((order) => (
            <option key={order.id} value={order.id}>
              № {order.id} · {order.lines_count} поз.
            </option>
          ))}
        </select>
      )}
      <button
        className="small"
        disabled={busy}
        title={`Дописать ${qty(row.qty)} шт. с приходом ${row.arrival_week} в черновик № ${target.id}`}
        onClick={() => onAdd(target.id, row)}
      >
        {open.length > 1 ? 'добавить' : `в черновик № ${target.id}`}
      </button>
      {already.length > 0 && (
        <span className="draft-note">
          уже в {already.map((order) => `№${order.id}`).join(', ')}
        </span>
      )}
    </span>
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
      {/* ROI — число, которое нельзя оставить без объяснения: иначе его либо
          не понимают, либо принимают за обещанную прибыль. */}
      {row.roi_percent != null && row.sell_out_weeks != null && row.cycle_weeks != null && (
        <p className="formula">
          вложено <b>{moneyFormat.format(row.cost)}</b>, маржа{' '}
          <b>
            {Math.round(((row.params.price - row.params.cost_price) / row.params.cost_price) * 100)}{' '}
            %
          </b>
          {', распродастся за '}
          <b>{row.sell_out_weeks.toFixed(1)} нед.</b>, деньги заняты{' '}
          <b>{row.cycle_weeks.toFixed(1)} нед.</b> с учётом срока поставки{' = '}
          <b>{Math.round(row.roi_percent)} % годовых</b>
          <span className="muted">
            {' '}
            — без комиссии маркетплейса, логистики и хранения: их в системе нет,
            поэтому число годится для сравнения позиций, а не как обещание доходности
          </span>
        </p>
      )}
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
