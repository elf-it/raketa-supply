import { useMemo } from 'react';

import type { Level, Row, Week } from '../types';
import { EditableCell } from './EditableCell';
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
  /** Роль «просмотр» видит те же числа, но ничего не правит. */
  canEdit: boolean;
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
  canEdit,
}: Props) {
  const months = useMemo(() => monthSpans(weeks), [weeks]);
  const metrics =
    level === 'sku'
      ? SKU_METRICS
      : [GROUP_METRIC_OPTIONS.find((m) => m.id === groupMetric) ?? GROUP_METRIC_OPTIONS[0]];

  return (
    <div className="matrix-scroll">
      <table className="matrix">
        <thead>
          <tr>
            <th className="col-name" rowSpan={2}>
              {level === 'sku' && rows.length > 0 && canEdit && (
                <input
                  type="checkbox"
                  className="row-check"
                  checked={selectedPairs.size === rows.length}
                  ref={(node) => {
                    if (node) {
                      node.indeterminate =
                        selectedPairs.size > 0 && selectedPairs.size < rows.length;
                    }
                  }}
                  onChange={onToggleAll}
                  title="Отметить все строки"
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
        <tbody>
          {rows.map((row) => (
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
            />
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty" colSpan={weeks.length + (level === 'sku' ? 2 : 1)}>
                Под фильтры ничего не попало
              </td>
            </tr>
          )}
        </tbody>
        <tfoot>
          <tr className="totals">
            <th className="col-name">{totals.title}</th>
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
}: RowBlockProps) {
  const editable =
    canEdit && level === 'sku' && row.meta.editable && row.meta.pair_id !== undefined;

  return (
    <>
      {metrics.map((metric, index) => (
        <tr
          key={metric.id}
          className={[
            index === metrics.length - 1 ? 'block-end' : '',
            selected ? 'selected' : '',
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
              <div className="row-title">{row.title}</div>
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
