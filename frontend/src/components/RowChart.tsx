import { useEffect, useId, useMemo, useRef, useState } from 'react';

import type { Row, Week } from '../types';

/**
 * График по выбранным строкам матрицы.
 *
 * Одна строка — подробный разбор: две панели с общей осью недель, но раздельными
 * вертикальными шкалами. Сверху недельный спрос (десятки штук), снизу сток (сотни).
 * На одной шкале столбики спроса превратились бы в полоску у нуля. Раздельные шкалы
 * означают, что точка пересечения столбика и линии ничего не значит — поэтому панели
 * и разведены, а не наложены друг на друга.
 *
 * Несколько строк — сравнение формы спроса и стока: по линии спроса и линии стока
 * на каждую строку. Подробности одной позиции при этом исчезают: столбики превращаются
 * в линии, штриховка непокрытого спроса и метки поставок не рисуются. Иначе четыре
 * позиции дали бы четыре набора столбиков в каждой неделе, и читать стало бы нечего.
 *
 * Данные берутся из тех же строк, что показывает таблица: отдельного запроса нет,
 * поэтому правка прогноза перерисовывает график сразу.
 */

/** Ширина недели. При 52 неделях график шире экрана и скроллится, как и матрица. */
const STEP = 26;
const BAR = 14;
const PAD_LEFT = 56;
const PAD_RIGHT = 16;
const TOP = 28;
const DEMAND_H = 150;
/** Полоса между панелями: маркеры поставок, номера недель и смена года. */
const AXIS_H = 46;
const STOCK_H = 112;

/**
 * Цвета сравнения. Их ровно столько, сколько строк помещается на график:
 * пятый цвет пришлось бы повторять, и две строки стали бы неразличимы.
 */
export const SERIES_COLORS = [
  'var(--series-1)',
  'var(--series-2)',
  'var(--series-3)',
  'var(--series-4)',
];

export const MAX_CHART_ROWS = SERIES_COLORS.length;

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 0 });

function num(value: number | boolean | null | undefined): number | null {
  return typeof value === 'number' ? value : null;
}

function fmt(value: number | null): string {
  return value === null ? '—' : numberFormat.format(Math.round(value));
}

/** Округление границы оси до «круглого» числа, иначе подписи шкалы нечитаемы. */
function niceBound(value: number): number {
  if (value <= 0) return 1;
  const pow = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.5, 2, 2.5, 5, 10]) {
    if (step * pow >= value) return step * pow;
  }
  return 10 * pow;
}

const x = (index: number) => PAD_LEFT + index * STEP + STEP / 2;

/** Ось недель, общая для обоих режимов: номера, смена года, зоны наведения. */
function WeekAxis({
  weeks,
  hover,
  onHover,
  bottom,
}: {
  weeks: Week[];
  hover: number | null;
  onHover: (index: number | null) => void;
  bottom: number;
}) {
  return (
    <>
      {weeks.map((week, i) => (
        <g key={`axis-${week.key}`}>
          {i % 2 === 0 && (
            <text x={x(i)} y={TOP + DEMAND_H + 24} className="axis-week">
              {String(week.iso_week).padStart(2, '0')}
            </text>
          )}
          {/* Номера недель начинаются заново — без года ось читалась бы неверно. */}
          {(i === 0 || week.year !== weeks[i - 1].year) && (
            <text x={x(i)} y={TOP + DEMAND_H + 38} className="axis-year">
              {week.year}
            </text>
          )}
          {i > 0 && week.year !== weeks[i - 1].year && (
            <line
              x1={PAD_LEFT + i * STEP}
              y1={TOP}
              x2={PAD_LEFT + i * STEP}
              y2={bottom}
              className="year-divider"
            />
          )}
          <rect
            x={PAD_LEFT + i * STEP}
            y={TOP - 6}
            width={STEP}
            height={bottom - TOP + 6}
            className={hover === i ? 'hover-band active' : 'hover-band'}
            onMouseEnter={() => onHover(i)}
          />
        </g>
      ))}
    </>
  );
}

/** Граница «факт / план»: первая незакрытая неделя. */
function FactPlanDivider({ weeks, bottom }: { weeks: Week[]; bottom: number }) {
  const firstOpen = weeks.findIndex((w) => !w.is_closed);
  if (firstOpen <= 0) return null;
  const at = PAD_LEFT + firstOpen * STEP;
  return (
    <>
      <line x1={at} y1={TOP - 6} x2={at} y2={bottom} className="chart-divider" />
      <text x={at - 5} y={TOP - 8} className="axis-title to-end">
        факт
      </text>
      <text x={at + 5} y={TOP - 8} className="axis-title">
        план
      </text>
    </>
  );
}

/** Подробный график одной строки: столбики спроса, сток с заливкой дефицита. */
function SingleChart({ row, weeks }: { row: Row; weeks: Week[] }) {
  const uid = useId().replace(/:/g, '');
  const [hover, setHover] = useState<number | null>(null);

  const points = useMemo(
    () =>
      weeks.map((week) => ({
        week,
        fact: num(row.metrics.fact_sales?.[week.key]),
        original: num(row.metrics.original_forecast?.[week.key]),
        adapted: num(row.metrics.adapted_forecast?.[week.key]),
        shortage: num(row.metrics.shortage_units?.[week.key]),
        supply: num(row.metrics.supply?.[week.key]),
        realStock: num(row.metrics.real_stock?.[week.key]),
        adaptedStock: num(row.metrics.adapted_stock?.[week.key]),
      })),
    [row, weeks],
  );

  const width = PAD_LEFT + weeks.length * STEP + PAD_RIGHT;
  const stockTop = TOP + DEMAND_H + AXIS_H;
  const height = stockTop + STOCK_H + 18;

  const demandMax = niceBound(
    Math.max(1, ...points.map((p) => Math.max(p.fact ?? 0, p.original ?? 0))),
  );
  const stockValues = points.flatMap((p) =>
    [p.realStock, p.adaptedStock].filter((v): v is number => v !== null),
  );
  const stockMax = niceBound(Math.max(1, ...stockValues, 0));
  // Реальный сток уходит в минус — это и есть дефицит, отрезать его нельзя.
  const stockFloor = Math.min(0, ...stockValues);
  const stockMin = stockFloor < 0 ? -niceBound(-stockFloor) : 0;

  const demandY = (value: number) => TOP + DEMAND_H - (value / demandMax) * DEMAND_H;
  const stockY = (value: number) =>
    stockTop + STOCK_H - ((value - stockMin) / (stockMax - stockMin)) * STOCK_H;
  const zeroY = stockY(0);

  const line = (pick: (p: (typeof points)[number]) => number | null) =>
    points
      .map((p, i) => (pick(p) === null ? null : `${x(i)},${stockY(pick(p) as number)}`))
      .filter((v): v is string => v !== null)
      .join(' ');

  const active = hover === null ? null : points[hover];

  return (
    <>
      <div className="chart-readout">
        {active ? (
          <>
            <strong>
              W{String(active.week.iso_week).padStart(2, '0')} {active.week.year}
            </strong>
            <span>
              факт <b>{fmt(active.fact)}</b>
            </span>
            <span>
              прогноз <b>{fmt(active.original)}</b>
            </span>
            <span className="adapted-text">
              адаптированный <b>{fmt(active.adapted)}</b>
            </span>
            {(active.shortage ?? 0) > 0 && (
              <span className="danger-text">
                не покрыто <b>{fmt(active.shortage)}</b>
              </span>
            )}
            {(active.supply ?? 0) > 0 && (
              <span>
                поставка <b>{fmt(active.supply)}</b>
              </span>
            )}
            <span>
              сток <b>{fmt(active.realStock)}</b>
            </span>
          </>
        ) : (
          <span className="muted">Наведите курсор на неделю, чтобы увидеть числа</span>
        )}
      </div>

      <div className="chart-scroll">
        <svg
          className="chart"
          width={width}
          height={height}
          role="img"
          aria-label={`График спроса и стока: ${row.title}`}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            {/* Площадь дефицита заливается только ниже нуля. */}
            <clipPath id={`${uid}-below`}>
              <rect x={0} y={zeroY} width={width} height={stockTop + STOCK_H - zeroY} />
            </clipPath>
            <pattern
              id={`${uid}-short`}
              width={4}
              height={4}
              patternTransform="rotate(45)"
              patternUnits="userSpaceOnUse"
            >
              <rect width={4} height={4} fill="var(--danger-soft)" />
              <line x1={0} y1={0} x2={0} y2={4} stroke="var(--danger)" strokeWidth={1.4} />
            </pattern>
          </defs>

          {/* Шкала спроса */}
          {[0, demandMax / 2, demandMax].map((tick) => (
            <g key={`d-${tick}`}>
              <line
                x1={PAD_LEFT}
                y1={demandY(tick)}
                x2={width - PAD_RIGHT}
                y2={demandY(tick)}
                className={tick === 0 ? 'axis-base' : 'axis-grid'}
              />
              <text x={PAD_LEFT - 8} y={demandY(tick) + 4} className="axis-label">
                {numberFormat.format(Math.round(tick))}
              </text>
            </g>
          ))}
          <text x={4} y={12} className="axis-title">
            спрос, шт / нед.
          </text>

          {/* Шкала стока */}
          {[stockMin, 0, stockMax]
            .filter((tick, index, all) => all.indexOf(tick) === index)
            .map((tick) => (
              <g key={`s-${tick}`}>
                <line
                  x1={PAD_LEFT}
                  y1={stockY(tick)}
                  x2={width - PAD_RIGHT}
                  y2={stockY(tick)}
                  className={tick === 0 ? 'axis-base' : 'axis-grid'}
                />
                <text x={PAD_LEFT - 8} y={stockY(tick) + 4} className="axis-label">
                  {numberFormat.format(Math.round(tick))}
                </text>
              </g>
            ))}
          <text x={4} y={stockTop - 8} className="axis-title">
            сток, шт
          </text>

          <FactPlanDivider weeks={weeks} bottom={stockTop + STOCK_H} />

          {/* Столбики спроса */}
          {points.map((p, i) => {
            const center = x(i);
            const closed = p.week.is_closed;
            const original = p.original ?? 0;
            const adapted = p.adapted ?? 0;
            const shortage = Math.max(0, p.shortage ?? 0);
            return (
              <g key={p.week.key}>
                {closed ? (
                  <>
                    {original > 0 && (
                      <rect
                        x={center - BAR / 2}
                        y={demandY(original)}
                        width={BAR}
                        height={Math.max(0, demandY(0) - demandY(original))}
                        className="bar-plan"
                      />
                    )}
                    {(p.fact ?? 0) > 0 && (
                      <rect
                        x={center - BAR / 2 + 2}
                        y={demandY(p.fact as number)}
                        width={BAR - 4}
                        height={Math.max(0, demandY(0) - demandY(p.fact as number))}
                        className="bar-fact"
                      />
                    )}
                  </>
                ) : (
                  <>
                    {adapted > 0 && (
                      <rect
                        x={center - BAR / 2}
                        y={demandY(adapted)}
                        width={BAR}
                        height={Math.max(0, demandY(0) - demandY(adapted))}
                        className="bar-adapted"
                      />
                    )}
                    {shortage > 0 && (
                      <rect
                        x={center - BAR / 2}
                        y={demandY(adapted + shortage)}
                        width={BAR}
                        height={Math.max(0, demandY(adapted) - demandY(adapted + shortage))}
                        fill={`url(#${uid}-short)`}
                        stroke="var(--danger)"
                        strokeWidth={0.6}
                      />
                    )}
                  </>
                )}
                {(p.supply ?? 0) > 0 && (
                  <polygon
                    points={`${center},${demandY(0) + 4} ${center - 4},${demandY(0) + 11} ${center + 4},${demandY(0) + 11}`}
                    className="supply-mark"
                  />
                )}
              </g>
            );
          })}

          {/* Сток: дефицит закрашивается ниже нуля */}
          <polygon
            points={`${x(0)},${zeroY} ${line((p) => p.realStock)} ${x(points.length - 1)},${zeroY}`}
            className="stock-deficit"
            clipPath={`url(#${uid}-below)`}
          />
          <polyline points={line((p) => p.adaptedStock)} className="stock-adapted" />
          <polyline points={line((p) => p.realStock)} className="stock-real" />

          <WeekAxis
            weeks={weeks}
            hover={hover}
            onHover={setHover}
            bottom={stockTop + STOCK_H}
          />
        </svg>
      </div>

      <div className="chart-legend">
        <span className="swatch plan" /> план закрытой недели
        <span className="swatch fact" /> факт продаж
        <span className="swatch adapted" /> адаптированный прогноз
        <span className="swatch short" /> не покрыто стоком
        <span className="swatch supply" /> поставка
        <span className="swatch real-line" /> реальный сток
        <span className="swatch adapted-line" /> адаптированный сток
      </div>
      <p className="footnote">
        У панелей разные вертикальные шкалы: спрос — десятки штук в неделю, сток — сотни.
        Поэтому сравнивать высоту столбика с высотой линии нельзя, только с другими столбиками.
        Реальный сток ниже нуля — это дефицит: спрос, который не обслужен.
      </p>
    </>
  );
}

interface Series {
  row: Row;
  color: string;
  demand: (number | null)[];
  stock: (number | null)[];
  demandPeak: number;
  stockPeak: number;
}

/** Как строки приводятся друг к другу на сравнении. */
type ScaleMode = 'units' | 'axes' | 'share';

const SCALE_MODES: { id: ScaleMode; label: string; hint: string }[] = [
  {
    id: 'units',
    label: 'Общая шкала',
    hint: 'Все строки в штуках на одной шкале: видно разницу в объёме, мелкая строка прижата к нулю.',
  },
  {
    id: 'axes',
    label: 'Две оси',
    hint:
      'У каждой строки своя шкала в штуках: левая и правая. Высоты линий между собой ' +
      'не сравнимы — сравнивается форма, а числа остаются настоящими.',
  },
  {
    id: 'share',
    label: 'Доля от пика',
    hint: 'Каждая строка приведена к своему пику: видно форму спроса, а не объём.',
  },
];

/** Шкала панели: верх, низ и цвет строки, если шкала принадлежит ей одной. */
interface Axis {
  max: number;
  min: number;
  color: string | null;
}

/**
 * Шкалы панели: одна общая на все строки или по одной на строку.
 *
 * У двух осей ноль стоит на одной высоте, поэтому доля шкалы под нулём берётся
 * наибольшая из двух. Иначе линия одной строки пересекала бы нулевую черту там,
 * где у неё ещё есть остаток, и «ниже нуля — дефицит» перестало бы читаться
 * хотя бы для одной из них.
 */
function buildAxes(perRow: number[][], split: boolean, colors: string[]): Axis[] {
  const bound = (values: number[]) => ({
    max: niceBound(Math.max(1, ...values, 0)),
    floor: Math.min(0, ...values),
  });
  if (!split) {
    const all = bound(perRow.flat());
    return [{ max: all.max, min: all.floor < 0 ? -niceBound(-all.floor) : 0, color: null }];
  }
  const bounds = perRow.map(bound);
  const ratio = Math.max(0, ...bounds.map((b) => -b.floor / b.max));
  return bounds.map((b, index) => ({
    max: b.max,
    min: -ratio * b.max,
    color: colors[index] ?? null,
  }));
}

/** Сравнение нескольких строк: по линии спроса и линии стока на каждую. */
function CompareChart({ rows, weeks }: { rows: Row[]; weeks: Week[] }) {
  const [hover, setHover] = useState<number | null>(null);
  /** Что выбрал человек; что показано на самом деле — `mode` ниже. */
  const [picked, setPicked] = useState<ScaleMode>('units');

  const series: Series[] = useMemo(
    () =>
      rows.map((row, index) => {
        // Спрос одной линией: на закрытых неделях факт, на открытых — исходный
        // прогноз. Именно исходный: адаптированный обрезан стоком и на позиции
        // с дефицитом падает в ноль, так что сравнивалась бы не форма спроса,
        // а способность его обслужить. Дефицит и так виден на панели стока.
        const demand = weeks.map((week) =>
          week.is_closed
            ? num(row.metrics.fact_sales?.[week.key])
            : num(row.metrics.original_forecast?.[week.key]),
        );
        const stock = weeks.map((week) => num(row.metrics.real_stock?.[week.key]));
        return {
          row,
          color: SERIES_COLORS[index % SERIES_COLORS.length],
          demand,
          stock,
          demandPeak: Math.max(1, ...demand.map((v) => Math.abs(v ?? 0))),
          stockPeak: Math.max(1, ...stock.map((v) => Math.abs(v ?? 0))),
        };
      }),
    [rows, weeks],
  );

  // Третью ось рисовать негде, поэтому режим двух осей живёт только на паре строк.
  // Выбор человека при этом сохраняется: уберёт лишнюю строку — вернётся к осям.
  const mode: ScaleMode = picked === 'axes' && series.length !== 2 ? 'share' : picked;
  const split = mode === 'axes';
  const share = mode === 'share';

  // В долях каждая строка делится на собственный пик, в штуках — остаётся как есть.
  const scaled = (value: number | null, peak: number) =>
    value === null ? null : share ? (value / peak) * 100 : value;

  // Справа место под подписи второй шкалы, и только в этом режиме.
  const padRight = split ? PAD_LEFT : PAD_RIGHT;
  const width = PAD_LEFT + weeks.length * STEP + padRight;
  const stockTop = TOP + DEMAND_H + AXIS_H;
  const height = stockTop + STOCK_H + 18;

  const colors = series.map((s) => s.color);
  const panelValues = (pick: (s: Series) => (number | null)[], peak: (s: Series) => number) =>
    series.map((s) =>
      pick(s)
        .map((value) => scaled(value, peak(s)))
        .filter((value): value is number => value !== null),
    );

  const demandAxes = buildAxes(
    panelValues((s) => s.demand, (s) => s.demandPeak),
    split,
    colors,
  );
  const stockAxes = buildAxes(
    panelValues((s) => s.stock, (s) => s.stockPeak),
    split,
    colors,
  );

  /** Одна шкала — она общая; две — у каждой строки своя. */
  const axisOf = (axes: Axis[], index: number) => axes[axes.length === 1 ? 0 : index];
  const yOn = (axis: Axis, top: number, panel: number) => (value: number) =>
    top + panel - ((value - axis.min) / (axis.max - axis.min)) * panel;

  const path = (
    values: (number | null)[],
    peak: number,
    y: (value: number) => number,
  ) =>
    values
      .map((value, i) => {
        const v = scaled(value, peak);
        return v === null ? null : `${x(i)},${y(v)}`;
      })
      .filter((v): v is string => v !== null)
      .join(' ');

  const tick = (value: number) =>
    share ? `${numberFormat.format(Math.round(value))}%` : numberFormat.format(Math.round(value));

  const at = (axis: Axis, fraction: number) => axis.min + fraction * (axis.max - axis.min);

  /**
   * Линии шкалы. Положение у них общее, а подписи — свои у каждой оси: доли
   * высоты совпадают, потому что ноль у обеих осей сведён на одну высоту.
   */
  const ticks = (axes: Axis[], top: number, panel: number, fractions: number[]) =>
    fractions.map((fraction) => {
      const y = top + panel - fraction * panel;
      return (
        <g key={`${top}-${fraction}`}>
          <line
            x1={PAD_LEFT}
            y1={y}
            x2={width - padRight}
            y2={y}
            className={Math.abs(at(axes[0], fraction)) < 0.5 ? 'axis-base' : 'axis-grid'}
          />
          <text
            x={PAD_LEFT - 8}
            y={y + 4}
            className="axis-label"
            style={axes[0].color ? { fill: axes[0].color } : undefined}
          >
            {tick(at(axes[0], fraction))}
          </text>
          {axes[1] && (
            <text
              x={width - padRight + 8}
              y={y + 4}
              className="axis-label to-start"
              style={axes[1].color ? { fill: axes[1].color } : undefined}
            >
              {tick(at(axes[1], fraction))}
            </text>
          )}
        </g>
      );
    });

  const zeroFraction = (axis: Axis) => (0 - axis.min) / (axis.max - axis.min);
  const stockFractions = [...new Set([0, zeroFraction(stockAxes[0]), 1])].sort((a, b) => a - b);

  return (
    <>
      <div className="chart-controls">
        <div className="switch">
          {SCALE_MODES.map((item) => (
            <button
              key={item.id}
              className={mode === item.id ? 'active' : ''}
              disabled={item.id === 'axes' && series.length !== 2}
              title={
                item.id === 'axes' && series.length !== 2
                  ? 'Две оси помещаются только для двух строк'
                  : item.hint
              }
              onClick={() => setPicked(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <span className="hint">{SCALE_MODES.find((item) => item.id === mode)?.hint}</span>
        {split && (
          <span className="hint axis-owners">
            <span className="swatch line" style={{ background: series[0].color }} />
            слева {series[0].row.title}
            <span className="swatch line" style={{ background: series[1].color }} />
            справа {series[1].row.title}
          </span>
        )}
        {picked === 'axes' && !split && (
          <span className="hint danger-text">
            Две оси помещаются только для двух строк: пока показаны доли от пика.
          </span>
        )}
      </div>

      <div className="chart-readout compare">
        {hover === null ? (
          <span className="muted">Наведите курсор на неделю, чтобы увидеть числа</span>
        ) : (
          <>
            <strong>
              W{String(weeks[hover].iso_week).padStart(2, '0')} {weeks[hover].year}
            </strong>
            {series.map((s) => (
              <span key={s.row.key} className="readout-row">
                <span className="swatch line" style={{ background: s.color }} />
                {s.row.title}: спрос <b>{fmt(s.demand[hover])}</b>, сток{' '}
                <b>{fmt(s.stock[hover])}</b>
              </span>
            ))}
          </>
        )}
      </div>

      <div className="chart-scroll">
        <svg
          className="chart"
          width={width}
          height={height}
          role="img"
          aria-label={`Сравнение строк: ${rows.map((r) => r.title).join(', ')}`}
          onMouseLeave={() => setHover(null)}
        >
          {/* Шкала спроса */}
          {ticks(demandAxes, TOP, DEMAND_H, [0, 0.5, 1])}
          <text x={4} y={12} className="axis-title">
            спрос{share ? ', % от пика' : ', шт / нед.'}
          </text>

          {/* Шкала стока */}
          {ticks(stockAxes, stockTop, STOCK_H, stockFractions)}
          <text x={4} y={stockTop - 8} className="axis-title">
            сток{share ? ', % от пика' : ', шт'}
          </text>

          {/* Правая шкала подписана цветом своей строки — чтобы не искать, чья она. */}
          {split && (
            <>
              <text
                x={width - padRight + 8}
                y={12}
                className="axis-title"
                style={{ fill: series[1].color }}
              >
                шт / нед.
              </text>
              <text
                x={width - padRight + 8}
                y={stockTop - 8}
                className="axis-title"
                style={{ fill: series[1].color }}
              >
                шт
              </text>
            </>
          )}

          <FactPlanDivider weeks={weeks} bottom={stockTop + STOCK_H} />

          {series.map((s, index) => (
            <g key={s.row.key}>
              <polyline
                points={path(
                  s.demand,
                  s.demandPeak,
                  yOn(axisOf(demandAxes, index), TOP, DEMAND_H),
                )}
                className="compare-line"
                stroke={s.color}
              />
              <polyline
                points={path(
                  s.stock,
                  s.stockPeak,
                  yOn(axisOf(stockAxes, index), stockTop, STOCK_H),
                )}
                className="compare-line"
                stroke={s.color}
              />
            </g>
          ))}

          <WeekAxis
            weeks={weeks}
            hover={hover}
            onHover={setHover}
            bottom={stockTop + STOCK_H}
          />
        </svg>
      </div>

      <p className="footnote">
        {split && (
          <>
            У строк разные шкалы, поэтому высоты линий между собой не сравнимы, а точка
            их пересечения ничего не значит: сравнивается форма, а подписи остаются
            настоящими штуками. Ноль у обеих осей сведён на одну высоту — иначе линия
            одной строки пересекала бы нулевую черту там, где у неё ещё есть остаток.{' '}
          </>
        )}
        Линия спроса — факт на закрытых неделях и исходный прогноз на открытых: он не
        обнуляется из-за дефицита, поэтому сравнивается спрос, а не остаток. Нижняя
        панель — реальный сток, он уходит ниже нуля там, где спрос не обслужен.
        Штриховка непокрытого спроса, план закрытой недели и метки поставок здесь
        не рисуются: они читаются только на графике одной строки. Чтобы вернуться
        к разбору позиции, снимите лишние строки.
      </p>
    </>
  );
}

interface Props {
  rows: Row[];
  weeks: Week[];
  /** Сообщение о том, почему строка не добавилась; живёт рядом с графиком. */
  notice?: string | null;
  onRemove: (key: string) => void;
  onClose: () => void;
}

export function RowChart({ rows, weeks, notice, onRemove, onClose }: Props) {
  const box = useRef<HTMLElement>(null);
  const single = rows.length === 1 ? rows[0] : null;

  // Строку выбирают щелчком по названию в таблице, и она может быть далеко внизу:
  // без этого график открылся бы за пределами экрана. `nearest` не двигает уже видимое.
  useEffect(() => {
    box.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [rows.length]);

  const totalShortage = single
    ? weeks.reduce((sum, week) => sum + (num(single.metrics.shortage_units?.[week.key]) ?? 0), 0)
    : 0;

  return (
    <section className="panel chart-panel" ref={box}>
      <div className="panel-head">
        <div>
          <h2>{single ? single.title : `Сравнение строк: ${rows.length}`}</h2>
          <p className="muted">
            {single ? (
              <>
                {single.subtitle ? `${single.subtitle} · ` : ''}
                спрос и сток по неделям
                {totalShortage > 0
                  ? ` · недопоставленный спрос за горизонт: ${fmt(totalShortage)} шт`
                  : ''}
              </>
            ) : (
              `Общая ось недель; шкалы — на выбор. Щелчок по названию строки в таблице
               добавляет её на график и убирает обратно; больше ${MAX_CHART_ROWS} строк
               не помещается.`
            )}
          </p>
        </div>
        <button className="link" onClick={onClose}>
          Скрыть график
        </button>
      </div>

      {!single && (
        <div className="chart-chips">
          {rows.map((row, index) => (
            <span key={row.key} className="chip">
              <span
                className="swatch line"
                style={{ background: SERIES_COLORS[index % SERIES_COLORS.length] }}
              />
              {row.title}
              <button
                className="link small"
                title="Убрать строку с графика"
                onClick={() => onRemove(row.key)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {notice && <p className="hint danger-text">{notice}</p>}

      {single ? (
        <SingleChart row={single} weeks={weeks} />
      ) : (
        <CompareChart rows={rows} weeks={weeks} />
      )}
    </section>
  );
}
