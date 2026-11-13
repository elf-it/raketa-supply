import { useCallback, useEffect, useState } from 'react';

import { api } from '../api';
import type { BasePotentialRow, MatrixFilters } from '../types';
import { EditableCell } from './EditableCell';

const FACT_WEEK_OPTIONS = [4, 8, 13, 26];

const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 1 });

function format(value: number | null): string {
  return value === null ? '—' : numberFormat.format(value);
}

interface Props {
  filters: Omit<MatrixFilters, 'level'>;
  onError: (message: string | null) => void;
  canEdit: boolean;
}

/**
 * Страница базового потенциала SKU (ТЗ, п. 03).
 *
 * Рядом с задаваемым значением показан факт закрытых недель и оценка потенциала,
 * полученная обратным ходом: факт делится на коэффициент сезонности той же недели.
 * Недели, в которых остаток упал до нуля, в оценку не входят — дефицит занижает факт.
 */
export function BasePotentialPage({ filters, onError, canEdit }: Props) {
  const [factWeeks, setFactWeeks] = useState(8);
  const [rows, setRows] = useState<BasePotentialRow[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [pending, setPending] = useState<Set<number>>(new Set());

  const reload = useCallback(async () => {
    try {
      setRows(await api.basePotential(filters, factWeeks));
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setLoaded(true);
    }
  }, [filters, factWeeks, onError]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = useCallback(
    async (pairId: number, value: number | null) => {
      if (value === null) return;
      setPending((prev) => new Set(prev).add(pairId));
      try {
        const updated = await api.setBasePotential(pairId, value);
        setRows((prev) => prev.map((row) => (row.pair_id === pairId ? updated : row)));
        onError(null);
      } catch (err) {
        onError((err as Error).message);
      } finally {
        setPending((prev) => {
          const next = new Set(prev);
          next.delete(pairId);
          return next;
        });
      }
    },
    [onError],
  );

  const applySuggested = async (row: BasePotentialRow) => {
    if (row.suggested_potential === null) return;
    await save(row.pair_id, Math.round(row.suggested_potential));
  };

  const gap = (row: BasePotentialRow): number | null => {
    if (row.suggested_potential === null || row.base_potential <= 0) return null;
    return (row.suggested_potential / row.base_potential - 1) * 100;
  };

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Базовый потенциал SKU</h2>
          <p className="muted">
            Недельный спрос без сезонности. Прогноз считается как потенциал × коэффициент
            категории, поэтому правка здесь меняет всю матрицу и закупку.
          </p>
        </div>
        <label className="inline-field">
          Ориентир по факту
          <select value={factWeeks} onChange={(event) => setFactWeeks(Number(event.target.value))}>
            {FACT_WEEK_OPTIONS.map((weeks) => (
              <option key={weeks} value={weeks}>
                {weeks} нед.
              </option>
            ))}
          </select>
        </label>
      </header>

      <div className="matrix-scroll">
        <table className="purchase">
          <thead>
            <tr>
              <th>Товар</th>
              <th>Магазин</th>
              <th>Категория</th>
              <th>Потенциал, шт/нед</th>
              <th>Факт, среднее</th>
              <th>Оценка по факту</th>
              <th>Расхождение</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const delta = gap(row);
              return (
                <tr key={row.pair_id} className={pending.has(row.pair_id) ? 'pending' : undefined}>
                  <td>
                    <div className="row-title">{row.title}</div>
                    <div className="row-subtitle">арт. {row.vendor_code}</div>
                  </td>
                  <td>{row.store_name}</td>
                  <td>{row.category ?? '—'}</td>
                  <td className="num">
                    {canEdit ? (
                      <EditableCell
                        value={row.base_potential}
                        onCommit={(value) => void save(row.pair_id, value)}
                      />
                    ) : (
                      <span className="value">{format(row.base_potential)}</span>
                    )}
                  </td>
                  <td className="num">
                    {format(row.avg_fact_units)}
                    <span className="hint">
                      {row.fact_weeks} нед.
                      {row.excluded_weeks > 0 && `, ${row.excluded_weeks} с нулевым остатком`}
                    </span>
                  </td>
                  <td className="num">{format(row.suggested_potential)}</td>
                  <td className={`num ${delta !== null && Math.abs(delta) > 20 ? 'danger' : ''}`}>
                    {delta === null ? '—' : `${delta > 0 ? '+' : '−'}${Math.abs(delta).toFixed(0)}%`}
                  </td>
                  <td>
                    {canEdit && (
                      <button
                        className="link"
                        disabled={row.suggested_potential === null}
                        onClick={() => void applySuggested(row)}
                      >
                        Принять оценку
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 && (
              <tr>
                <td className="empty" colSpan={8}>
                  {loaded ? 'Под фильтры ничего не попало' : 'Загружаю…'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <p className="footnote">
        Оценка — это факт, делённый на коэффициент сезонности той же недели. Недели, где остаток
        упал до нуля, в неё не входят: дефицит занижает продажи и утянул бы потенциал вниз.
        Это ориентир, а не замена решению снабженца.
      </p>
    </section>
  );
}
