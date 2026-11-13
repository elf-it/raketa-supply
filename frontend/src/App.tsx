import { useCallback, useEffect, useMemo, useState } from 'react';

import { api, setUnauthorizedHandler } from './api';
import { BasePotentialPage } from './components/BasePotentialPage';
import { BulkPanel } from './components/BulkPanel';
import { JournalPage } from './components/JournalPage';
import { KpiBar } from './components/KpiBar';
import { LoginPage } from './components/LoginPage';
import { PurchasePanel } from './components/PurchasePanel';
import { SeasonalityPage } from './components/SeasonalityPage';
import { GROUP_METRICS, WeekMatrix } from './components/WeekMatrix';
import type {
  BulkRequest,
  Category,
  CurrentUser,
  Level,
  Matrix,
  PurchasePlan,
  Role,
  Store,
} from './types';

const ROLE_LABELS: Record<Role, string> = {
  viewer: 'Просмотр',
  editor: 'Редактирование',
  admin: 'Администратор',
};

const LEVELS: { id: Level; label: string }[] = [
  { id: 'store', label: 'Магазины' },
  { id: 'category', label: 'Категории' },
  { id: 'sku', label: 'Артикулы' },
];

type Tab = 'matrix' | 'purchase' | 'potential' | 'seasonality' | 'journal';

const TABS: { id: Tab; label: string }[] = [
  { id: 'matrix', label: 'Матрица' },
  { id: 'purchase', label: 'Закупка' },
  { id: 'potential', label: 'Потенциал' },
  { id: 'seasonality', label: 'Сезонность' },
  { id: 'journal', label: 'Журнал' },
];

/** На каких экранах имеет смысл общая панель фильтров. */
const FILTERED_TABS: Tab[] = ['matrix', 'purchase', 'potential', 'journal'];

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  const [level, setLevel] = useState<Level>('sku');
  const [storeId, setStoreId] = useState<number | null>(null);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [groupMetric, setGroupMetric] = useState('original_forecast');
  const [tab, setTab] = useState<Tab>('matrix');

  const [stores, setStores] = useState<Store[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [purchase, setPurchase] = useState<PurchasePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingCells, setPendingCells] = useState<Set<string>>(new Set());
  const [selectedPairs, setSelectedPairs] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);

  // Стабильные ссылки: иначе дочерние экраны перезапрашивают данные на каждый рендер.
  const pageFilters = useMemo(
    () => ({ storeId, categoryId, search: debouncedSearch }),
    [storeId, categoryId, debouncedSearch],
  );
  const filters = useMemo(() => ({ ...pageFilters, level }), [pageFilters, level]);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search), 300);
    return () => clearTimeout(timer);
  }, [search]);

  // Истёкшая сессия возвращает на экран входа из любого места приложения.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setUser(null);
      setSessionExpired(true);
    });
  }, []);

  useEffect(() => {
    api
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setAuthChecked(true));
  }, []);

  const canEdit = user?.role === 'editor' || user?.role === 'admin';

  useEffect(() => {
    if (!user) return;
    Promise.all([api.stores(), api.categories()])
      .then(([s, c]) => {
        setStores(s);
        setCategories(c);
      })
      .catch((err: Error) => setError(err.message));
  }, [user]);

  const reload = useCallback(async () => {
    // Экраны потенциала и сезонности грузят себя сами.
    if (!user || (tab !== 'matrix' && tab !== 'purchase')) return;
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
  }, [filters, tab, user]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Отметки относятся к конкретной выборке строк: при смене фильтров они бессмысленны.
  useEffect(() => {
    setSelectedPairs(new Set());
  }, [pageFilters, level]);

  const skuRowIds = useMemo(
    () =>
      matrix?.level === 'sku'
        ? matrix.rows
            .map((row) => row.meta.pair_id)
            .filter((id): id is number => id !== undefined)
        : [],
    [matrix],
  );

  const toggleRow = useCallback((pairId: number) => {
    setSelectedPairs((prev) => {
      const next = new Set(prev);
      if (next.has(pairId)) next.delete(pairId);
      else next.add(pairId);
      return next;
    });
  }, []);

  const toggleAll = useCallback(() => {
    setSelectedPairs((prev) =>
      prev.size === skuRowIds.length ? new Set() : new Set(skuRowIds),
    );
  }, [skuRowIds]);

  const applyBulk = useCallback(
    async (request: BulkRequest) => {
      setBulkBusy(true);
      try {
        await api.bulkAdjust(request);
        setMatrix(await api.matrix(filters));
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBulkBusy(false);
      }
    },
    [filters],
  );

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

  const signOut = async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
      setSessionExpired(false);
      setMatrix(null);
      setPurchase(null);
    }
  };

  if (!authChecked) {
    return <div className="boot">Загружаю…</div>;
  }

  if (!user) {
    return (
      <LoginPage
        expired={sessionExpired}
        onSignedIn={(signedIn) => {
          setUser(signedIn);
          setSessionExpired(false);
          setError(null);
        }}
      />
    );
  }

  return (
    <div className="app">
      <header className="app-head">
        <div>
          <h1>Недельный план продаж</h1>
          <p className="muted">План, факт, новый прогноз и сток в единой недельной матрице</p>
        </div>
        <div className="head-actions">
          <span className="who">
            {user.full_name}
            <span className={`badge role ${user.role}`}>{ROLE_LABELS[user.role]}</span>
          </span>
          <button className="link" onClick={() => void signOut()}>
            Выйти
          </button>
          {canEdit && (
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
          )}
          <div className="tabs">
            {TABS.map((item) => (
              <button
                key={item.id}
                className={tab === item.id ? 'active' : ''}
                onClick={() => setTab(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="filters" hidden={!FILTERED_TABS.includes(tab)}>
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

          {matrix.level === 'sku' && canEdit && (
            <BulkPanel
              weeks={matrix.weeks}
              pairIds={selectedPairs.size > 0 ? [...selectedPairs] : skuRowIds}
              selectionCount={selectedPairs.size}
              totalRows={skuRowIds.length}
              busy={bulkBusy}
              onApply={applyBulk}
              onClearSelection={() => setSelectedPairs(new Set())}
            />
          )}

          <WeekMatrix
            level={matrix.level}
            weeks={matrix.weeks}
            rows={matrix.rows}
            totals={matrix.totals}
            groupMetric={groupMetric}
            onEdit={handleEdit}
            pendingCells={pendingCells}
            selectedPairs={selectedPairs}
            onToggleRow={toggleRow}
            onToggleAll={toggleAll}
            canEdit={canEdit}
          />

          <p className="footnote">
            Новый прогноз редактируется в таблице, после закрытия недели он фиксируется как её план.
            Исходный спрос не обнуляется из-за дефицита: обрезается только адаптированный прогноз.
          </p>
        </>
      )}

      {tab === 'purchase' && purchase && <PurchasePanel plan={purchase} />}

      {tab === 'potential' && (
        <BasePotentialPage filters={pageFilters} onError={setError} canEdit={canEdit} />
      )}

      {tab === 'seasonality' && <SeasonalityPage onError={setError} canEdit={canEdit} />}

      {tab === 'journal' && <JournalPage filters={pageFilters} onError={setError} />}
    </div>
  );
}
