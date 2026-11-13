import { useCallback, useEffect, useMemo, useState } from 'react';

import { api } from './api';
import { KpiBar } from './components/KpiBar';
import { PurchasePanel } from './components/PurchasePanel';
import { GROUP_METRICS, WeekMatrix } from './components/WeekMatrix';
import type { Category, Level, Matrix, PurchasePlan, Store } from './types';

const LEVELS: { id: Level; label: string }[] = [
  { id: 'store', label: 'Магазины' },
  { id: 'category', label: 'Категории' },
  { id: 'sku', label: 'Артикулы' },
];

export default function App() {
  const [level, setLevel] = useState<Level>('sku');
  const [storeId, setStoreId] = useState<number | null>(null);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [groupMetric, setGroupMetric] = useState('original_forecast');
  const [tab, setTab] = useState<'matrix' | 'purchase'>('matrix');

  const [stores, setStores] = useState<Store[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [purchase, setPurchase] = useState<PurchasePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingCells, setPendingCells] = useState<Set<string>>(new Set());

  const filters = useMemo(
    () => ({ level, storeId, categoryId, search: debouncedSearch }),
    [level, storeId, categoryId, debouncedSearch],
  );

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    Promise.all([api.stores(), api.categories()])
      .then(([s, c]) => {
        setStores(s);
        setCategories(c);
      })
      .catch((err: Error) => setError(err.message));
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      if (tab === 'matrix') {
        setMatrix(await api.matrix(filters));
      } else {
        setPurchase(await api.purchase(filters));
      }
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [filters, tab]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const handleEdit = useCallback(
    async (pairId: number, week: string, field: string, value: number | null) => {
      const metricId = field === 'diff_percent' ? 'diff_percent' : 'original_forecast';
      const cellKey = `${pairId}:${week}:${metricId}`;
      setPendingCells((prev) => new Set(prev).add(cellKey));
      try {
        await api.setForecast(
          pairId,
          week,
          value === null ? { clear: [field] } : { [field]: value },
        );
        // Изменение прогноза пересчитывает остатки и закупку — перечитываем целиком.
        setMatrix(await api.matrix(filters));
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setPendingCells((prev) => {
          const next = new Set(prev);
          next.delete(cellKey);
          return next;
        });
      }
    },
    [filters],
  );

  return (
    <div className="app">
      <header className="app-head">
        <div>
          <h1>Недельный план продаж</h1>
          <p className="muted">План, факт, новый прогноз и сток в единой недельной матрице</p>
        </div>
        <div className="head-actions">
          <button
            className="ghost"
            onClick={async () => {
              setLoading(true);
              try {
                await api.syncWb();
                await reload();
              } catch (err) {
                setError((err as Error).message);
              } finally {
                setLoading(false);
              }
            }}
          >
            Синхронизировать WB
          </button>
          <div className="tabs">
            <button
              className={tab === 'matrix' ? 'active' : ''}
              onClick={() => setTab('matrix')}
            >
              Матрица
            </button>
            <button
              className={tab === 'purchase' ? 'active' : ''}
              onClick={() => setTab('purchase')}
            >
              Закупка
            </button>
          </div>
        </div>
      </header>

      <div className="filters">
        <input
          className="search"
          placeholder="Товар или артикул"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          value={storeId ?? ''}
          onChange={(event) => setStoreId(event.target.value ? Number(event.target.value) : null)}
        >
          <option value="">Все магазины</option>
          {stores.map((store) => (
            <option key={store.id} value={store.id}>
              {store.name}
            </option>
          ))}
        </select>
        <select
          value={categoryId ?? ''}
          onChange={(event) =>
            setCategoryId(event.target.value ? Number(event.target.value) : null)
          }
        >
          <option value="">Все ниши и категории</option>
          {categories.map((category) => (
            <option key={category.id} value={category.id}>
              {category.niche ? `${category.niche} · ${category.name}` : category.name}
            </option>
          ))}
        </select>
        {tab === 'matrix' && level !== 'sku' && (
          <select value={groupMetric} onChange={(event) => setGroupMetric(event.target.value)}>
            {GROUP_METRICS.map((metric) => (
              <option key={metric.id} value={metric.id}>
                {metric.label}
              </option>
            ))}
          </select>
        )}
        {loading && <span className="spinner">обновляю…</span>}
      </div>

      {error && <div className="error">Ошибка запроса: {error}</div>}

      {tab === 'matrix' && matrix && (
        <>
          <KpiBar kpi={matrix.kpi} />

          <div className="level-switch">
            <span className="level-label">Уровень просмотра</span>
            {LEVELS.map((item) => (
              <button
                key={item.id}
                className={level === item.id ? 'active' : ''}
                onClick={() => setLevel(item.id)}
              >
                {item.label}
              </button>
            ))}
            <span className="muted level-hint">
              {matrix.context.sku_count} SKU · закрыта {matrix.context.last_closed_week} · горизонт{' '}
              {matrix.context.horizon_weeks} нед.
            </span>
          </div>

          <WeekMatrix
            level={matrix.level}
            weeks={matrix.weeks}
            rows={matrix.rows}
            totals={matrix.totals}
            groupMetric={groupMetric}
            onEdit={handleEdit}
            pendingCells={pendingCells}
          />

          <p className="footnote">
            Новый прогноз редактируется в таблице, после закрытия недели он фиксируется как её план.
            Исходный спрос не обнуляется из-за дефицита: обрезается только адаптированный прогноз.
          </p>
        </>
      )}

      {tab === 'purchase' && purchase && <PurchasePanel plan={purchase} />}
    </div>
  );
}
