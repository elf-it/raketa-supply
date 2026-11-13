import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from '../api';
import type { SeasonalityRow } from '../types';

const WEEKS = Array.from({ length: 53 }, (_, index) => index + 1);
/** Высота столбика нормируется на этот коэффициент. */
const BAR_SCALE = 2;

function currentIsoWeek(today = new Date()): number {
  const date = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate()));
  // Четверг той же недели определяет ISO-год.
  date.setUTCDate(date.getUTCDate() + 4 - (date.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(date.getUTCFullYear(), 0, 1));
  return Math.ceil(((date.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
}

function parseCoefficient(raw: string): number | null {
  const parsed = Number(raw.trim().replace(',', '.'));
  if (Number.isNaN(parsed) || parsed < 0 || parsed > 10) return null;
  return parsed;
}

interface Props {
  onError: (message: string | null) => void;
  canEdit: boolean;
}

/**
 * Страница недельных коэффициентов сезонности категорий (ТЗ, п. 03).
 *
 * Правки копятся локально и уходят на сервер одной кнопкой: 53 поля — это слишком
 * много, чтобы бить сеть запросом на каждую ячейку.
 */
export function SeasonalityPage({ onError, canEdit }: Props) {
  const [rows, setRows] = useState<SeasonalityRow[]>([]);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [draft, setDraft] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState(false);
  const [rangeFrom, setRangeFrom] = useState(1);
  const [rangeTo, setRangeTo] = useState(53);
  const [rangeValue, setRangeValue] = useState('1.00');

  const thisWeek = useMemo(() => currentIsoWeek(), []);

  const load = useCallback(async () => {
    try {
      const data = await api.seasonality();
      setRows(data);
      setCategoryId((prev) => prev ?? data[0]?.category_id ?? null);
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [onError]);

  useEffect(() => {
    void load();
  }, [load]);

  const current = rows.find((row) => row.category_id === categoryId) ?? null;

  const saved = useMemo(() => {
    const out: Record<number, number> = {};
    for (const week of WEEKS) out[week] = current?.coefficients[String(week)] ?? 1;
    return out;
  }, [current]);

  // При смене категории черновик обнуляется, иначе правки утекут в чужие коэффициенты.
  useEffect(() => {
    setDraft({});
  }, [categoryId]);

  const value = (week: number): string =>
    draft[week] ?? saved[week].toFixed(2);

  const changed = useMemo(() => {
    const out: Record<number, number> = {};
    for (const [key, raw] of Object.entries(draft)) {
      const week = Number(key);
      const parsed = parseCoefficient(raw);
      if (parsed !== null && Math.abs(parsed - saved[week]) > 1e-9) out[week] = parsed;
    }
    return out;
  }, [draft, saved]);

  const invalid = useMemo(
    () => Object.values(draft).some((raw) => parseCoefficient(raw) === null),
    [draft],
  );

  const changedCount = Object.keys(changed).length;

  const applyRange = () => {
    const parsed = parseCoefficient(rangeValue);
    if (parsed === null) {
      onError('Коэффициент должен быть числом от 0 до 10');
      return;
    }
    const from = Math.min(rangeFrom, rangeTo);
    const to = Math.max(rangeFrom, rangeTo);
    setDraft((prev) => {
      const next = { ...prev };
      for (let week = from; week <= to; week += 1) next[week] = parsed.toFixed(2);
      return next;
    });
    onError(null);
  };

  const save = async () => {
    if (categoryId === null || changedCount === 0) return;
    setSaving(true);
    try {
      const updated = await api.setSeasonality(categoryId, changed);
      setRows((prev) =>
        prev.map((row) => (row.category_id === categoryId ? updated : row)),
      );
      setDraft({});
      onError(null);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>Сезонность категорий</h2>
          <p className="muted">
            Коэффициент на номер ISO-недели. Исходный прогноз = базовый потенциал SKU ×
            коэффициент его категории, поэтому правка меняет прогноз всех товаров категории.
          </p>
        </div>
        {canEdit && (
          <div className="panel-totals">
            <button className="ghost" onClick={() => setDraft({})} disabled={changedCount === 0}>
              Отменить правки
            </button>
            <button
              className="primary"
              onClick={() => void save()}
              disabled={changedCount === 0 || invalid || saving}
            >
              {saving ? 'Сохраняю…' : `Сохранить${changedCount ? ` (${changedCount})` : ''}`}
            </button>
          </div>
        )}
      </header>

      <div className="season-controls">
        <label className="inline-field">
          Категория
          <select
            value={categoryId ?? ''}
            onChange={(event) => setCategoryId(Number(event.target.value))}
          >
            {rows.map((row) => (
              <option key={row.category_id} value={row.category_id}>
                {row.category}
              </option>
            ))}
          </select>
        </label>

        <div className="range-apply" hidden={!canEdit}>
          <span className="muted">Задать диапазону</span>
          <input
            type="number"
            min={1}
            max={53}
            value={rangeFrom}
            onChange={(event) => setRangeFrom(Number(event.target.value))}
          />
          <span className="muted">—</span>
          <input
            type="number"
            min={1}
            max={53}
            value={rangeTo}
            onChange={(event) => setRangeTo(Number(event.target.value))}
          />
          <input
            className="coef"
            value={rangeValue}
            onChange={(event) => setRangeValue(event.target.value)}
          />
          <button onClick={applyRange}>Применить</button>
        </div>
      </div>

      {invalid && <p className="inline-warning">Коэффициент — число от 0 до 10</p>}

      <div className="season-grid">
        {WEEKS.map((week) => {
          const raw = value(week);
          const parsed = parseCoefficient(raw);
          const height = parsed === null ? 0 : Math.min(parsed / BAR_SCALE, 1) * 100;
          const isChanged = week in changed;
          return (
            <div
              key={week}
              className={[
                'season-cell',
                week === thisWeek ? 'current' : '',
                isChanged ? 'changed' : '',
                parsed === null ? 'invalid' : '',
              ].join(' ')}
            >
              <div className="season-week">W{String(week).padStart(2, '0')}</div>
              <div className="season-bar" aria-hidden="true">
                <div
                  className={parsed !== null && parsed > 1 ? 'fill high' : 'fill'}
                  style={{ height: `${height}%` }}
                />
              </div>
              <input
                value={raw}
                inputMode="decimal"
                readOnly={!canEdit}
                onChange={(event) =>
                  setDraft((prev) => ({ ...prev, [week]: event.target.value }))
                }
                onFocus={(event) => canEdit && event.target.select()}
              />
            </div>
          );
        })}
      </div>

      <p className="footnote">
        53-я неделя есть не в каждом году — её коэффициент применяется только там, где она
        существует. Столбик выше середины означает коэффициент больше единицы.
      </p>
    </section>
  );
}
