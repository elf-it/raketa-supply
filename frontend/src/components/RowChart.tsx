import { useEffect, useId, useMemo, useRef, useState } from 'react';

import type { Row, Week } from '../types';

/**
 * График выбранной строки матрицы.
 *
 * Две панели с общей осью недель, но раздельными вертикальными шкалами: сверху
 * недельный спрос (десятки штук), снизу сток (сотни). На одной шкале столбики
 * спроса превратились бы в полоску у нуля. Раздельные шкалы означают, что точка
 * пересечения столбика и линии ничего не значит — поэтому панели и разведены,
 * а не наложены друг на друга.
 *
 * Данные берутся из той же строки, что показывает таблица: отдельного запроса нет,
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

interface Props {
  row: Row;
  weeks: Week[];
  onClose: () => void;
}

export function RowChart({ row, weeks, onClose }: Props) {
  const uid = useId().replace(/:/g, '');
  const [hover, setHover] = useState<number | null>(null);
  const box = useRef<HTMLElement>(null);

  // Строку выбирают щелчком по названию в таблице, и она может быть далеко внизу:
  // без этого график открылся бы за пределами экрана. `nearest` не двигает уже видимое.
  useEffect(() => {
    box.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [row.key]);

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

  const x = (index: number) => PAD_LEFT + index * STEP + STEP / 2;
  const demandY = (value: number) => TOP + DEMAND_H - (value / demandMax) * DEMAND_H;
  const stockY = (value: number) =>
    stockTop + STOCK_H - ((value - stockMin) / (stockMax - stockMin)) * STOCK_H;
  const zeroY = stockY(0);

  const line = (pick: (p: (typeof points)[number]) => number | null) =>
    points
      .map((p, i) => (pick(p) === null ? null : `${x(i)},${stockY(pick(p) as number)}`))
      .filter((v): v is string => v !== null)
      .join(' ');

  // Граница «факт / план»: первая незакрытая неделя.
  const firstOpen = weeks.findIndex((w) => !w.is_closed);
  const dividerX = firstOpen > 0 ? PAD_LEFT + firstOpen * STEP : null;

  const active = hover === null ? null : points[hover];
  const totalShortage = points.reduce((sum, p) => sum + (p.shortage ?? 0), 0);

  return (
    <section className="panel chart-panel" ref={box}>
      <div className="panel-head">
        <div>
          <h2>{row.title}</h2>
          <p className="muted">
            {row.subtitle ? `${row.subtitle} · ` : ''}
            спрос и сток по неделям
            {totalShortage > 0
              ? ` · недопоставленный спрос за горизонт: ${fmt(totalShortage)} шт`
              : ''}
          </p>
        </div>
        <button className="link" onClick={onClose}>
          Скрыть график
        </button>
      </div>

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

          {dividerX !== null && (
            <>
              <line
                x1={dividerX}
                y1={TOP - 6}
                x2={dividerX}
                y2={stockTop + STOCK_H}
                className="chart-divider"
              />
              <text x={dividerX - 5} y={TOP - 8} className="axis-title to-end">
                факт
              </text>
              <text x={dividerX + 5} y={TOP - 8} className="axis-title">
                план
              </text>
            </>
          )}

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

          {/* Подписи недель и зоны наведения */}
          {points.map((p, i) => (
            <g key={`axis-${p.week.key}`}>
              {i % 2 === 0 && (
                <text x={x(i)} y={TOP + DEMAND_H + 24} className="axis-week">
                  {String(p.week.iso_week).padStart(2, '0')}
                </text>
              )}
              {/* Номера недель начинаются заново — без года ось читалась бы неверно. */}
              {(i === 0 || p.week.year !== points[i - 1].week.year) && (
                <text x={x(i)} y={TOP + DEMAND_H + 38} className="axis-year">
                  {p.week.year}
                </text>
              )}
              {i > 0 && p.week.year !== points[i - 1].week.year && (
                <line
                  x1={PAD_LEFT + i * STEP}
                  y1={TOP}
                  x2={PAD_LEFT + i * STEP}
                  y2={stockTop + STOCK_H}
                  className="year-divider"
                />
              )}
              <rect
                x={PAD_LEFT + i * STEP}
                y={TOP - 6}
                width={STEP}
                height={stockTop + STOCK_H - TOP + 6}
                className={hover === i ? 'hover-band active' : 'hover-band'}
                onMouseEnter={() => setHover(i)}
              />
            </g>
          ))}
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
    </section>
  );
}
