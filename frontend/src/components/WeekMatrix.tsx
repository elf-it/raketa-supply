import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import type { Level, Row, Week } from '../types';
import { EditableCell } from './EditableCell';
import { SERIES_COLORS } from './RowChart';
import { StatusBadge } from './StatusBadge';

/** Строка показателя внутри блока позиции. */
interface MetricDef {
  id: string;
  label: string;
  /** Поле правки на бэкенде; пусто — показатель только для чтения. */
  editable?: 'manual_forecast' | 'diff_percent';
  suffix?: string;
  tone?: 'adapted' | 'muted' | 'strong';
}

const SKU_METRICS: MetricDef[] = [
  { id: 'original_forecast', label: 'План / исходный прогноз', editable: 'manual_forecast', tone: 'strong' },
  { id: 'adapted_forecast', label: 'Адаптированный прогноз', tone: 'adapted' },
  { id: 'fact_sales', label: 'Факт продаж', tone: 'muted' },
  { id: 'diff_percent', label: 'DIFF', editable: 'diff_percent', suffix: '%', tone: 'muted' },
  { id: 'real_stock', label: 'Реальный сток' },
  { id: 'adapted_stock', label: 'Адаптированный сток', tone: 'adapted' },
];

const GROUP_METRIC_OPTIONS: MetricDef[] = [
  { id: 'original_forecast', label: 'Исходный прогноз', tone: 'strong' },
  { id: 'adapted_forecast', label: 'Адаптированный прогноз', tone: 'adapted' },
  { id: 'fact_sales', label: 'Факт продаж', tone: 'muted' },
  { id: 'real_stock', label: 'Реальный сток' },
  { id: 'adapted_stock', label: 'Адаптированный сток', tone: 'adapted' },
  { id: 'shortage_units', label: 'Недопоставленный спрос' },
];

export const GROUP_METRICS = GROUP_METRIC_OPTIONS;

/**
 * Виртуализация строк.
 *
 * На тысяче артикулов таблица — это сотни тысяч ячеек: браузер их создаёт,
 * размечает и держит в памяти, и один щелчок по строке стоит секунд десять
 * пересборки. Поэтому в DOM живут только блоки, попавшие в окно прокрутки,
 * а место остальных занимают две распорки — пустые строки нужной высоты.
 * Полоса прокрутки от этого не врёт: общая высота таблицы та же.
 *
 * Цена решения: поиск по странице (Ctrl+F) и печать видят только то, что
 * сейчас в DOM. Поиск по артикулу и названию в системе свой, серверный,
 * а печать идёт через выгрузку в XLSX — поэтому обмен признан равноценным.
 *
 * Строки при этом приходят не все сразу: сервер отдаёт окно выборки, и таблица
 * просит следующее, когда прокрутка подошла к концу загруженного. Виртуализация
 * и постраничная выдача — разные вещи: первая решает, сколько строк живёт в DOM,
 * вторая — сколько их приехало по сети.
 */
const OVERSCAN = 3;
/** Ниже этого числа строк таблица рисуется целиком: виртуализация не бесплатна. */
const VIRTUALIZE_FROM = 60;
/** Сколько блоков отрисовать первым заходом, чтобы было что измерить. */
const PROBE_BLOCKS = 8;

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function formatValue(value: number | boolean | null | undefined): string {
  if (value === null || value === undefined || typeof value === 'boolean') return '—';
  return numberFormat.format(Math.round(value));
}

function monthSpans(weeks: Week[]): { label: string; span: number }[] {
  const spans: { label: string; span: number }[] = [];
  for (const week of weeks) {
    const last = spans[spans.length - 1];
    if (last && last.label === week.month_label) last.span += 1;
    else spans.push({ label: week.month_label, span: 1 });
  }
  return spans;
}

interface Props {
  level: Level;
  weeks: Week[];
  rows: Row[];
  totals: Row;
  groupMetric: string;
  onEdit: (pairId: number, week: string, field: string, value: number | null) => void;
  pendingCells: Set<string>;
  /** Отметки строк для массовых правок; доступны только на уровне артикулов. */
  selectedPairs: Set<number>;
  onToggleRow: (pairId: number) => void;
  onToggleAll: () => void;
  /**
   * Сколько строк можно отметить во всей выборке, а не в загруженной её части:
   * «отметить все» работает по списку пар с сервера, и флажок в шапке должен
   * показывать состояние этого же списка.
   */
  selectableCount: number;
  /** Роль «просмотр» видит те же числа, но ничего не правит. */
  canEdit: boolean;
  /**
   * Строки, показанные на графике, в порядке добавления: щелчок по названию
   * добавляет строку и убирает обратно. Порядок задаёт цвет линии при сравнении.
   */
  chartKeys: string[];
  onPickRow: (key: string) => void;
  /** Сколько строк в выборке целиком: `rows` — загруженная её часть. */
  rowCount: number;
  loadingMore: boolean;
  /** Просит следующую страницу строк. Вызывается и прокруткой, и кнопкой. */
  onNeedMore: () => void;
}

export function WeekMatrix({
  level,
  weeks,
  rows,
  totals,
  groupMetric,
  onEdit,
  pendingCells,
  selectedPairs,
  onToggleRow,
  onToggleAll,
  selectableCount,
  canEdit,
  chartKeys,
  onPickRow,
  rowCount,
  loadingMore,
  onNeedMore,
}: Props) {
  // Цвет строки появляется только при сравнении: на графике одной строки цветная
  // точка в таблице обозначала бы линию, которой там нет.
  const chartColor = (key: string) => {
    const index = chartKeys.indexOf(key);
    return index >= 0 && chartKeys.length > 1 ? SERIES_COLORS[index] : null;
  };

  const months = useMemo(() => monthSpans(weeks), [weeks]);
  const metrics =
    level === 'sku'
      ? SKU_METRICS
      : [GROUP_METRIC_OPTIONS.find((m) => m.id === groupMetric) ?? GROUP_METRIC_OPTIONS[0]];

  const scrollRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLTableSectionElement>(null);
  const [blockH, setBlockH] = useState(0);
  const [bodyTop, setBodyTop] = useState(0);
  const [view, setView] = useState({ top: 0, height: 0, width: 0 });
  const measuredFor = useRef('');

  // Виртуализируется только уровень артикулов: там все блоки одного строения,
  // значит и одной высоты — её можно измерить у отрисованных и распространить
  // на остальные. На уровнях групп строк столько, сколько магазинов и категорий,
  // а высота строки зависит от длины названия: мерить нечего и незачем.
  const virtual = level === 'sku' && rows.length >= VIRTUALIZE_FROM;

  useEffect(() => {
    const box = scrollRef.current;
    if (!virtual || !box) return;
    // Состояние обновляется прямо в обработчике, без requestAnimationFrame:
    // в скрытой вкладке кадры не рисуются, и окно так и осталось бы нулевым.
    // Лишних перерисовок нет — при тех же числах React получает прежний объект.
    const update = () => {
      setView((prev) =>
        prev.top === box.scrollTop &&
        prev.height === box.clientHeight &&
        prev.width === box.clientWidth
          ? prev
          : { top: box.scrollTop, height: box.clientHeight, width: box.clientWidth },
      );
    };
    update();
    box.addEventListener('scroll', update, { passive: true });
    // Высота окна меняется и без прокрутки: разворот панели, поворот экрана.
    const observer = new ResizeObserver(update);
    observer.observe(box);
    return () => {
      box.removeEventListener('scroll', update);
      observer.disconnect();
    };
  }, [virtual]);

  // Строки приходят страницами: сервер отдаёт окно выборки, остальное догружается.
  const hasMore = rows.length < rowCount;
  const needMore = useRef(onNeedMore);
  useEffect(() => {
    needMore.current = onNeedMore;
  }, [onNeedMore]);

  // Дозапрос привязан к прокрутке, а не только к кнопке: виртуализация и так
  // живёт на прокрутке, и человек просто продолжает листать. Запас — экран
  // высоты, чтобы страница пришла раньше, чем строки кончатся. Первый вызов
  // без прокрутки нужен для случая, когда загруженное помещается целиком:
  // тогда таблица добирает страницы, пока не появится, что прокручивать.
  useEffect(() => {
    const box = scrollRef.current;
    if (!hasMore || !box) return;
    const check = () => {
      if (box.scrollHeight - box.scrollTop - box.clientHeight < box.clientHeight) {
        needMore.current();
      }
    };
    check();
    box.addEventListener('scroll', check, { passive: true });
    return () => box.removeEventListener('scroll', check);
  }, [hasMore, rows.length]);

  const ready = virtual && blockH > 0 && view.height > 0;
  const first = ready ? Math.max(0, Math.floor((view.top - bodyTop) / blockH) - OVERSCAN) : 0;
  const last = ready
    ? Math.min(rows.length, first + Math.ceil(view.height / blockH) + OVERSCAN * 2)
    : virtual
      ? Math.min(rows.length, PROBE_BLOCKS)
      : rows.length;
  const shown = virtual ? rows.slice(first, last) : rows;
  const padTop = ready ? first * blockH : 0;
  const padBottom = ready ? (rows.length - last) * blockH : 0;
  const colSpan = weeks.length + (level === 'sku' ? 2 : 1);

  // Что меняет высоту блока: уровень, число показателей и недель, ширина окна
  // (от неё переносится название) и сама выборка строк. Прокрутка — не меняет.
  const measureKey = `${level}|${metrics.length}|${weeks.length}|${rows.length}|${Math.round(view.width)}x${Math.round(view.height)}`;

  // Замер после отрисовки: высота блока — средняя по показанным, чтобы одна
  // строка с длинным названием не растянула оценку на всю тысячу. Мерится один
  // раз на раскладку: пересчёт на каждой прокрутке менял бы высоту распорки
  // на доли пикселя, а на тысячной строке это сотня пикселей прыжка.
  useLayoutEffect(() => {
    const box = scrollRef.current;
    const body = bodyRef.current;
    if (!virtual || !box || !body) return;
    if (measuredFor.current === measureKey && blockH > 0) return;
    const starts = body.querySelectorAll<HTMLTableRowElement>('tr[data-block]');
    if (starts.length < 2) return;
    measuredFor.current = measureKey;
    const span =
      starts[starts.length - 1].getBoundingClientRect().top -
      starts[0].getBoundingClientRect().top;
    const height = span / (starts.length - 1);
    const top = body.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop;
    if (height > 0 && Math.abs(height - blockH) > 0.5) setBlockH(height);
    if (Math.abs(top - bodyTop) > 0.5) setBodyTop(top);
  }, [virtual, measureKey, blockH, bodyTop]);

  return (
    <div className="matrix-scroll" ref={scrollRef}>
      <table className="matrix">
        <thead>
          <tr>
            <th className="col-name" rowSpan={2}>
              {level === 'sku' && selectableCount > 0 && canEdit && (
                <input
                  type="checkbox"
                  className="row-check"
                  checked={selectedPairs.size === selectableCount}
                  ref={(node) => {
                    if (node) {
                      node.indeterminate =
                        selectedPairs.size > 0 && selectedPairs.size < selectableCount;
                    }
                  }}
                  onChange={onToggleAll}
                  title={`Отметить все строки выборки (${selectableCount})`}
                />
              )}
              {level === 'store' ? 'Магазин' : level === 'category' ? 'Категория' : 'Артикул / товар'}
            </th>
            {level === 'sku' && (
              <th className="col-metric" rowSpan={2}>
                Показатель
              </th>
            )}
            {months.map((month, index) => (
              <th key={`${month.label}-${index}`} colSpan={month.span} className="month">
                {month.label}
              </th>
            ))}
          </tr>
          <tr>
            {weeks.map((week) => (
              <th
                key={week.key}
                className={[
                  'week',
                  week.is_closed ? 'closed' : 'planned',
                  week.is_current ? 'current' : '',
                ].join(' ')}
                title={`Неделя начинается ${week.start}`}
              >
                W{String(week.iso_week).padStart(2, '0')}
                <span className="week-year">{week.year}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody ref={bodyRef}>
          {padTop > 0 && (
            <tr className="spacer" aria-hidden="true">
              <td colSpan={colSpan} style={{ height: padTop }} />
            </tr>
          )}
          {shown.map((row) => (
            <RowBlock
              key={row.key}
              row={row}
              level={level}
              weeks={weeks}
              metrics={metrics}
              onEdit={onEdit}
              pendingCells={pendingCells}
              selected={
                row.meta.pair_id !== undefined && selectedPairs.has(row.meta.pair_id)
              }
              onToggleRow={onToggleRow}
              canEdit={canEdit}
              charted={chartKeys.includes(row.key)}
              chartColor={chartColor(row.key)}
              onPick={() => onPickRow(row.key)}
            />
          ))}
          {padBottom > 0 && (
            <tr className="spacer" aria-hidden="true">
              <td colSpan={colSpan} style={{ height: padBottom }} />
            </tr>
          )}
          {hasMore && (
            <tr className="more">
              <td colSpan={colSpan}>
                <button className="ghost small" onClick={onNeedMore} disabled={loadingMore}>
                  {loadingMore ? 'Подгружаю…' : 'Показать ещё'}
                </button>
                <span className="muted">
                  показано {rows.length} из {rowCount} — остальные подгружаются при прокрутке
                </span>
              </td>
            </tr>
          )}
          {rows.length === 0 && !hasMore && (
            <tr>
              <td className="empty" colSpan={colSpan}>
                Под фильтры ничего не попало
              </td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr className={`totals ${chartKeys.includes(totals.key) ? 'charted' : ''}`}>
            <th className="col-name">
              {chartColor(totals.key) && (
                <span className="swatch line" style={{ background: chartColor(totals.key) ?? '' }} />
              )}
              <button
                className="row-title pick"
                onClick={() => onPickRow(totals.key)}
                title="График по итогу выборки"
              >
                {totals.title}
              </button>
            </th>
            {level === 'sku' && <th className="col-metric">{metrics[0].label}</th>}
            {weeks.map((week) => (
              <td key={week.key} className={week.is_closed ? 'closed' : 'planned'}>
                {formatValue(totals.metrics[metrics[0].id]?.[week.key])}
              </td>
            ))}
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

interface RowBlockProps {
  row: Row;
  level: Level;
  weeks: Week[];
  metrics: MetricDef[];
  onEdit: Props['onEdit'];
  pendingCells: Set<string>;
  selected: boolean;
  onToggleRow: (pairId: number) => void;
  canEdit: boolean;
  charted: boolean;
  /** Цвет линии этой строки на графике сравнения; `null` — строка не сравнивается. */
  chartColor: string | null;
  onPick: () => void;
}

function RowBlock({
  row,
  level,
  weeks,
  metrics,
  onEdit,
  pendingCells,
  selected,
  onToggleRow,
  canEdit,
  charted,
  chartColor,
  onPick,
}: RowBlockProps) {
  const editable =
    canEdit && level === 'sku' && row.meta.editable && row.meta.pair_id !== undefined;

  return (
    <>
      {metrics.map((metric, index) => (
        <tr
          key={metric.id}
          /* Метка первой строки блока: по ней виртуализация меряет высоту блока. */
          data-block={index === 0 ? row.key : undefined}
          className={[
            index === metrics.length - 1 ? 'block-end' : '',
            selected ? 'selected' : '',
            charted ? 'charted' : '',
          ].join(' ')}
        >
          {index === 0 && (
            <th className="col-name" rowSpan={metrics.length}>
              {editable && (
                <input
                  type="checkbox"
                  className="row-check"
                  checked={selected}
                  onChange={() => onToggleRow(row.meta.pair_id as number)}
                />
              )}
              {chartColor && <span className="swatch line" style={{ background: chartColor }} />}
              <button
                className="row-title pick"
                onClick={onPick}
                title={charted ? 'Убрать строку с графика' : 'Показать на графике'}
              >
                {row.title}
              </button>
              {row.subtitle && <div className="row-subtitle">{row.subtitle}</div>}
              <div className="row-badges">
                {row.meta.abc_class && <span className="badge abc">{row.meta.abc_class}</span>}
                {row.meta.store_name && level === 'sku' && (
                  <span className="badge store">{row.meta.store_name}</span>
                )}
                <StatusBadge status={row.status} />
              </div>
            </th>
          )}
          {level === 'sku' && <th className={`col-metric ${metric.tone ?? ''}`}>{metric.label}</th>}
          {weeks.map((week) => {
            const value = row.metrics[metric.id]?.[week.key];
            const supply = row.metrics.supply?.[week.key];
            const limited = row.metrics.stock_limited?.[week.key] === true;
            const manual = row.metrics.is_manual?.[week.key] === true;
            const cellKey = `${row.meta.pair_id}:${week.key}:${metric.id}`;

            const classes = [
              week.is_closed ? 'closed' : 'planned',
              week.is_current ? 'current' : '',
              metric.tone ?? '',
              pendingCells.has(cellKey) ? 'pending' : '',
            ];

            const canEdit = Boolean(editable && metric.editable && !week.is_closed);

            return (
              <td key={week.key} className={classes.join(' ')}>
                {canEdit ? (
                  <EditableCell
                    value={typeof value === 'number' ? value : null}
                    suffix={metric.suffix}
                    highlighted={metric.editable === 'manual_forecast' && manual}
                    onCommit={(next) =>
                      onEdit(row.meta.pair_id as number, week.key, metric.editable as string, next)
                    }
                  />
                ) : (
                  <span className="value">{formatValue(value)}</span>
                )}
                {metric.id === 'original_forecast' && typeof supply === 'number' && supply > 0 && (
                  <span className="supply">+{numberFormat.format(supply)}</span>
                )}
                {metric.id === 'adapted_forecast' && limited && (
                  <span className="limit">лимит стока</span>
                )}
              </td>
            );
          })}
        </tr>
      ))}
    </>
  );
}
