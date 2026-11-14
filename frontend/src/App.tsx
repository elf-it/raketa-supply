import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, setUnauthorizedHandler } from './api';
import { BasePotentialPage } from './components/BasePotentialPage';
import { BulkPanel } from './components/BulkPanel';
import { ChangePassword } from './components/ChangePassword';
import { JournalPage } from './components/JournalPage';
import { KpiBar } from './components/KpiBar';
import { LoginPage } from './components/LoginPage';
import { OrdersPage } from './components/OrdersPage';
import { PurchasePanel } from './components/PurchasePanel';
import { MAX_CHART_ROWS, RowChart } from './components/RowChart';
import { SeasonalityPage } from './components/SeasonalityPage';
import { UsersPage } from './components/UsersPage';
import { GROUP_METRICS, WeekMatrix } from './components/WeekMatrix';
import type {
  BulkRequest,
  Category,
  CurrentUser,
  Level,
  Matrix,
  PurchasePlan,
  PurchaseRow,
  Role,
  Row,
  Status,
  Store,
} from './types';

/** Отбор строк по статусу. Слова те же, что на значке строки, чтобы не плодить словарь. */
const STATUS_OPTIONS: { id: Status; label: string }[] = [
  { id: 'SHORT', label: 'Shortage — дефицит' },
  { id: 'OVER', label: 'Overstock — избыток' },
  { id: 'OK', label: 'ОК — норма' },
];

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

type Tab = 'matrix' | 'purchase' | 'orders' | 'potential' | 'seasonality' | 'journal' | 'users';

const TABS: { id: Tab; label: string; adminOnly?: boolean }[] = [
  { id: 'matrix', label: 'Матрица' },
  { id: 'purchase', label: 'Закупка' },
  { id: 'orders', label: 'Заказы' },
  { id: 'potential', label: 'Потенциал' },
  { id: 'seasonality', label: 'Сезонность' },
  { id: 'journal', label: 'Журнал' },
  { id: 'users', label: 'Пользователи', adminOnly: true },
];

/** На каких экранах имеет смысл общая панель фильтров. */
const FILTERED_TABS: Tab[] = ['matrix', 'purchase', 'potential', 'journal'];

/**
 * Сколько строк матрицы просить за раз.
 *
 * Выборка приходит окнами: на тысяче позиций ответ целиком — шесть мегабайт,
 * и браузер разбирает их раньше, чем успевает показать первую строку. Страница
 * в двести строк на горизонте в тридцать недель — около мегабайта: первый экран
 * появляется сразу, остальное подгружается при прокрутке.
 */
const PAGE_ROWS = 200;

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [changingPassword, setChangingPassword] = useState(false);

  const [level, setLevel] = useState<Level>('sku');
  const [storeId, setStoreId] = useState<number | null>(null);
  const [categoryId, setCategoryId] = useState<number | null>(null);
  const [search, setSearch] = useState('');
  const [debouncedSearch, setDebouncedSearch] = useState('');
  const [groupMetric, setGroupMetric] = useState('original_forecast');
  const [rowStatus, setRowStatus] = useState<Status | null>(null);
  const [tab, setTab] = useState<Tab>('matrix');

  const [stores, setStores] = useState<Store[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [purchase, setPurchase] = useState<PurchasePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pendingCells, setPendingCells] = useState<Set<string>>(new Set());
  const [selectedPairs, setSelectedPairs] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [chartKeys, setChartKeys] = useState<string[]>([]);
  const [chartNotice, setChartNotice] = useState<string | null>(null);
  const [onlyNeeded, setOnlyNeeded] = useState(true);
  const [creatingOrder, setCreatingOrder] = useState(false);

  /** Сколько строк матрицы уже загружено: страницы идут подряд от начала выборки. */
  const loadedRows = useRef(0);
  /**
   * Номер последнего запроса матрицы. Ответ более старого запроса выбрасывается:
   * иначе страница, заказанная до смены фильтров, приклеилась бы к новой выборке.
   */
  const requestSeq = useRef(0);
  /** Защёлка дозапроса: прокрутка зовёт его десятками событий подряд. */
  const moreBusy = useRef(false);

  // Стабильные ссылки: иначе дочерние экраны перезапрашивают данные на каждый рендер.
  const pageFilters = useMemo(
    () => ({ storeId, categoryId, search: debouncedSearch }),
    [storeId, categoryId, debouncedSearch],
  );
  // Статус — результат расчёта, поэтому он живёт только в фильтрах матрицы:
  // экраны потенциала, журнала и закупки о нём ничего не знают.
  const filters = useMemo(
    () => ({ ...pageFilters, level, rowStatus }),
    [pageFilters, level, rowStatus],
  );

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
    const seq = ++requestSeq.current;
    try {
      if (tab === 'matrix') {
        const fresh = await api.matrix(filters, { limit: PAGE_ROWS });
        if (seq !== requestSeq.current) return;
        loadedRows.current = fresh.rows.length;
        setMatrix(fresh);
      } else {
        setPurchase(await api.purchase(filters, onlyNeeded));
      }
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      // Запрос мог устареть, пока ждали ответ: тогда полосу «обновляю» снимает
      // тот, кто пришёл позже.
      if (seq === requestSeq.current) setLoading(false);
    }
  }, [filters, onlyNeeded, tab, user]);

  /**
   * Перечитывает уже загруженные строки после правки.
   *
   * Правка прогноза меняет не только свою ячейку: пересчитываются сток, статус
   * строки, итоги и KPI. Страницы идут подряд от начала выборки, поэтому вернуть
   * экран в прежний вид можно одним запросом на всю загруженную длину.
   */
  const refresh = useCallback(async () => {
    const seq = ++requestSeq.current;
    const fresh = await api.matrix(filters, { limit: Math.max(PAGE_ROWS, loadedRows.current) });
    if (seq !== requestSeq.current) return;
    loadedRows.current = fresh.rows.length;
    setMatrix(fresh);
  }, [filters]);

  /**
   * Обновляет одну строку после правки её ячейки.
   *
   * Правка меняет сток, статус и покрытие только у своей позиции — пары
   * независимы друг от друга, — а вот итоги и KPI считаются по всей выборке
   * и меняются тоже. Сервер присылает ровно это: строку, итоги, KPI и счётчики.
   * Перечитывать ради одной строки всё загруженное незачем: на четырёх страницах
   * это мегабайты на каждое нажатие.
   *
   * Перечитать всё приходится в трёх случаях, и все три видны по ответу: строки
   * в нём нет (отбор по статусу вытолкнул её из выборки, и за ней сдвинулись
   * остальные), строка стоит не на том месте, где она на экране, или сменилась
   * ось недель — например, кто-то закрыл неделю, пока мы правили.
   */
  const refreshRow = useCallback(
    async (pairId: number) => {
      const seq = ++requestSeq.current;
      const fresh = await api.matrixRow(filters, pairId);
      if (seq !== requestSeq.current) return;
      const updated = fresh.rows[0];
      const index = matrix && updated ? matrix.rows.findIndex((r) => r.key === updated.key) : -1;
      const sameAxis =
        matrix !== null &&
        fresh.weeks.length === matrix.weeks.length &&
        fresh.weeks[0]?.key === matrix.weeks[0]?.key;
      if (!updated || index < 0 || index !== fresh.context.row_offset || !sameAxis) {
        await refresh();
        return;
      }
      setMatrix((prev) => {
        if (!prev) return prev;
        const rows = [...prev.rows];
        rows[index] = updated;
        return {
          ...prev,
          rows,
          totals: fresh.totals,
          kpi: fresh.kpi,
          // Счётчики и список пар — из свежего ответа, окно строк — прежнее:
          // на экране по-прежнему те же страницы, что и были.
          context: {
            ...fresh.context,
            row_offset: prev.context.row_offset,
            row_limit: prev.context.row_limit,
          },
        };
      });
    },
    [filters, matrix, refresh],
  );

  /** Следующая страница строк: её просит таблица, когда прокрутка подошла к концу. */
  const loadMore = useCallback(async () => {
    if (moreBusy.current || !matrix) return;
    const offset = loadedRows.current;
    if (offset >= matrix.context.row_count) return;
    moreBusy.current = true;
    setLoadingMore(true);
    const seq = requestSeq.current;
    try {
      const next = await api.matrix(filters, { offset, limit: PAGE_ROWS });
      if (seq !== requestSeq.current) return;
      setMatrix((prev) => {
        // Страница встаёт только на свой стык: в любом другом случае выборка
        // под ней уже изменилась, и приклеивать строки некуда.
        if (!prev || next.context.row_offset !== prev.rows.length) return prev;
        const rows = [...prev.rows, ...next.rows];
        loadedRows.current = rows.length;
        // Итоги и KPI берутся из свежего ответа — они по всей выборке и не
        // зависят от окна; окно переписывается на то, что теперь на экране.
        return { ...next, rows, context: { ...next.context, row_offset: 0, row_limit: rows.length } };
      });
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      moreBusy.current = false;
      setLoadingMore(false);
    }
  }, [filters, matrix]);

  useEffect(() => {
    void reload();
  }, [reload]);

  // Отметки относятся к конкретной выборке строк: при смене фильтров они бессмысленны.
  useEffect(() => {
    setSelectedPairs(new Set());
    setChartKeys([]);
    setChartNotice(null);
  }, [pageFilters, level, rowStatus]);

  // График живёт на строках текущей матрицы, а не на своей копии данных: после правки
  // прогноза матрица перечитывается, и график перерисовывается тем же числам.
  // Ключи, которых в новой выборке нет, просто выпадают — отдельной чистки не нужно.
  const chartRows = useMemo(() => {
    if (!matrix) return [];
    const byKey = new Map([matrix.totals, ...matrix.rows].map((row) => [row.key, row]));
    return chartKeys.map((key) => byKey.get(key)).filter((row): row is Row => row !== undefined);
  }, [matrix, chartKeys]);

  /**
   * Щелчок по названию строки добавляет её на график и убирает обратно. Отдельного
   * режима сравнения нет: одна строка — разбор позиции, несколько — сравнение.
   */
  const pickRow = useCallback(
    (key: string) => {
      if (chartKeys.includes(key)) {
        setChartNotice(null);
        setChartKeys(chartKeys.filter((item) => item !== key));
        return;
      }
      if (chartKeys.length >= MAX_CHART_ROWS) {
        // Это не ошибка запроса, поэтому и не в общую полосу ошибок: сообщение
        // живёт у графика, рядом со строками, одну из которых нужно снять.
        setChartNotice(
          `На графике помещается ${MAX_CHART_ROWS} строки: снимите лишнюю, ` +
            'чтобы добавить новую',
        );
        return;
      }
      setChartNotice(null);
      setChartKeys([...chartKeys, key]);
    },
    [chartKeys],
  );

  // Пары всей выборки, а не только загруженных строк: сервер присылает их список
  // целиком. Иначе «отметить все» и массовая правка «ко всем строкам под фильтрами»
  // относились бы к той странице, до которой человек успел доскроллить.
  const skuRowIds = useMemo(
    () => (matrix?.level === 'sku' ? (matrix.context.pair_ids ?? []) : []),
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
        await refresh();
        setError(null);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBulkBusy(false);
      }
    },
    [refresh],
  );

  const createOrder = useCallback(async (rows: PurchaseRow[]) => {
    setCreatingOrder(true);
    try {
      await api.createOrder({
        lines: rows.map((row) => ({
          pair_id: row.pair_id,
          qty: row.qty,
          arrival_week: row.arrival_week ?? '',
        })),
      });
      setError(null);
      // Черновик ничего не меняет в расчёте, но смотреть на него нужно на своём экране.
      setTab('orders');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setCreatingOrder(false);
    }
  }, []);

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
        // Изменение прогноза пересчитывает остатки, статус и итоги, но только
        // у этой позиции: обновляем её строку, а не всё загруженное.
        await refreshRow(pairId);
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
    [refreshRow],
  );

  const signOut = async () => {
    try {
      await api.logout();
    } finally {
      setUser(null);
      setSessionExpired(false);
      setChangingPassword(false);
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
          <button className="link" onClick={() => setChangingPassword((open) => !open)}>
            Сменить пароль
          </button>
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
            {/* Вкладка пользователей — для администратора. Это удобство, а не защита:
                ручки /auth/users закрыты ролью на сервере. */}
            {TABS.filter((item) => !item.adminOnly || user.role === 'admin').map((item) => (
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

      {changingPassword && <ChangePassword onClose={() => setChangingPassword(false)} />}

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
        {tab === 'matrix' && (
          <select
            value={rowStatus ?? ''}
            onChange={(event) => setRowStatus((event.target.value || null) as Status | null)}
            title="Отбор строк по статусу"
          >
            <option value="">Все статусы</option>
            {STATUS_OPTIONS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        )}
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
              {/* При отборе по статусу видно, сколько строк он скрыл: иначе короткая
                  таблица выглядит как потерянные данные. */}
              {rowStatus
                ? `${matrix.context.sku_count} из ${matrix.context.sku_count_total} SKU`
                : `${matrix.context.sku_count} SKU`}{' '}
              · закрыта {matrix.context.last_closed_week} · горизонт{' '}
              {matrix.context.horizon_weeks} нед.
            </span>
            {/* Ссылка, а не fetch: файл сохраняет браузер под именем от сервера. */}
            <a
              className="button-link small"
              href={api.matrixExportUrl(filters)}
              title="Те же строки и недели, что на экране, в книге Excel"
            >
              Скачать .xlsx
            </a>
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

          {chartRows.length > 0 && (
            <RowChart
              rows={chartRows}
              weeks={matrix.weeks}
              notice={chartNotice}
              onRemove={pickRow}
              onClose={() => {
                setChartNotice(null);
                setChartKeys([]);
              }}
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
            selectableCount={skuRowIds.length}
            canEdit={canEdit}
            chartKeys={chartKeys}
            onPickRow={pickRow}
            rowCount={matrix.context.row_count}
            loadingMore={loadingMore}
            onNeedMore={() => void loadMore()}
          />

          <p className="footnote">
            Щелчок по названию строки или по итогу открывает график спроса и стока,
            следующий щелчок по другой строке добавляет её на тот же график для сравнения.
            Новый прогноз редактируется в таблице, после закрытия недели он фиксируется как её план.
            Исходный спрос не обнуляется из-за дефицита: обрезается только адаптированный прогноз.
          </p>
        </>
      )}

      {tab === 'purchase' && purchase && (
        <PurchasePanel
          plan={purchase}
          exportUrl={api.purchaseExportUrl(pageFilters, onlyNeeded)}
          onlyNeeded={onlyNeeded}
          onOnlyNeededChange={setOnlyNeeded}
          canEdit={canEdit}
          onCreateOrder={(rows) => void createOrder(rows)}
          creatingOrder={creatingOrder}
        />
      )}

      {tab === 'orders' && (
        <OrdersPage
          canEdit={canEdit}
          onError={setError}
          onChanged={() => {
            // Размещение и отмена меняют товары в пути: пересчитанные данные
            // должны ждать на экранах закупки и матрицы, а не прошлые.
            setMatrix(null);
            setPurchase(null);
          }}
        />
      )}

      {tab === 'potential' && (
        <BasePotentialPage filters={pageFilters} onError={setError} canEdit={canEdit} />
      )}

      {tab === 'seasonality' && <SeasonalityPage onError={setError} canEdit={canEdit} />}

      {tab === 'journal' && <JournalPage filters={pageFilters} onError={setError} />}

      {tab === 'users' && user.role === 'admin' && (
        <UsersPage
          me={user}
          onMeUpdated={(updated) => {
            setUser(updated);
            // Администратор мог снять права с самого себя: вкладка пропадает,
            // и оставлять его на пустом экране незачем.
            if (updated.role !== 'admin') setTab('matrix');
          }}
          onError={setError}
        />
      )}
    </div>
  );
}
