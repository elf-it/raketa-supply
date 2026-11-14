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

/** Сравнение нескольких строк: по линии спроса и линии стока на каждую. */
function CompareChart({ rows, weeks }: { rows: Row[]; weeks: Week[] }) {
  const [hover, setHover] = useState<number | null>(null);
  /** Доли вместо штук: иначе мелкая позиция лежит на нуле рядом с крупной. */
  const [share, setShare] = useState(false);

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

  // В долях каждая строка делится на собственный пик, в штуках — остаётся как есть.
  const scaled = (value: number | null, peak: number) =>
    value === null ? null : share ? (value / peak) * 100 : value;

  const width = PAD_LEFT + weeks.length * STEP + PAD_RIGHT;
  const stockTop = TOP + DEMAND_H + AXIS_H;
  const height = stockTop + STOCK_H + 18;

  const demandValues = series.flatMap((s) =>
    s.demand.map((v) => scaled(v, s.demandPeak)).filter((v): v is number => v !== null),
  );
  const stockValues = series.flatMap((s) =>
    s.stock.map((v) => scaled(v, s.stockPeak)).filter((v): v is number => v !== null),
  );
  const demandMax = niceBound(Math.max(1, ...demandValues));
  const stockMax = niceBound(Math.max(1, ...stockValues, 0));
  const stockFloor = Math.min(0, ...stockValues);
  const stockMin = stockFloor < 0 ? -niceBound(-stockFloor) : 0;

  const demandY = (value: number) => TOP + DEMAND_H - (value / demandMax) * DEMAND_H;
  const stockY = (value: number) =>
    stockTop + STOCK_H - ((value - stockMin) / (stockMax - stockMin)) * STOCK_H;

  const path = (values: (number | null)[], peak: number, y: (v: number) => number) =>
    values
      .map((value, i) => {
        const v = scaled(value, peak);
        return v === null ? null : `${x(i)},${y(v)}`;
      })
      .filter((v): v is string => v !== null)
      .join(' ');

  const tick = (value: number) =>
    share ? `${numberFormat.format(Math.round(value))}%` : numberFormat.format(Math.round(value));

  return (
    <>
      <div className="chart-controls">
        <label className="checkbox">
          <input type="checkbox" checked={share} onChange={() => setShare(!share)} />
          Доля от собственного максимума
        </label>
        <span className="hint">
          {share
            ? 'Каждая строка приведена к своему пику: видно форму спроса, а не объём.'
            : 'Общая шкала в штуках: видно разницу в объёме, мелкая строка прижата к нулю.'}
        </span>
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
          {[0, demandMax / 2, demandMax].map((value) => (
            <g key={`d-${value}`}>
              <line
                x1={PAD_LEFT}
                y1={demandY(value)}
                x2={width - PAD_RIGHT}
                y2={demandY(value)}
                className={value === 0 ? 'axis-base' : 'axis-grid'}
              />
              <text x={PAD_LEFT - 8} y={demandY(value) + 4} className="axis-label">
                {tick(value)}
              </text>
            </g>
          ))}
          <text x={4} y={12} className="axis-title">
            спрос{share ? ', % от пика' : ', шт / нед.'}
          </text>

          {/* Шкала стока */}
          {[stockMin, 0, stockMax]
            .filter((value, index, all) => all.indexOf(value) === index)
            .map((value) => (
              <g key={`s-${value}`}>
                <line
                  x1={PAD_LEFT}
                  y1={stockY(value)}
                  x2={width - PAD_RIGHT}
                  y2={stockY(value)}
                  className={value === 0 ? 'axis-base' : 'axis-grid'}
                />
                <text x={PAD_LEFT - 8} y={stockY(value) + 4} className="axis-label">
                  {tick(value)}
                </text>
              </g>
            ))}
          <text x={4} y={stockTop - 8} className="axis-title">
            сток{share ? ', % от пика' : ', шт'}
          </text>

          <FactPlanDivider weeks={weeks} bottom={stockTop + STOCK_H} />

          {series.map((s) => (
            <g key={s.row.key}>
              <polyline
                points={path(s.demand, s.demandPeak, demandY)}
                className="compare-line"
                stroke={s.color}
              />
              <polyline
                points={path(s.stock, s.stockPeak, stockY)}
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
              `Общая ось недель и общие шкалы. Щелчок по названию строки в таблице
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
