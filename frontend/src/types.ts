export type Role = 'viewer' | 'editor' | 'admin';

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: Role;
  is_active: boolean;
  /** Когда завели учётную запись; в JSON — строка с зоной. */
  created_at: string;
}

/** Тот, кто вошёл: та же запись, что и в списке пользователей. */
export type CurrentUser = User;

export type Level = 'store' | 'category' | 'sku';
export type Status = 'OK' | 'SHORT' | 'OVER';

export interface Week {
  key: string;
  year: number;
  iso_week: number;
  start: string;
  month_label: string;
  is_closed: boolean;
  is_current: boolean;
}

/** Значения показателя по неделям; `null` — нет данных (например, факт на будущей неделе). */
export type MetricSeries = Record<string, number | boolean | null>;

export interface Row {
  key: string;
  title: string;
  subtitle: string | null;
  status: Status;
  meta: {
    pair_id?: number;
    store_id?: number;
    store_name?: string;
    product_id?: number;
    category?: string | null;
    abc_class?: string | null;
    nm_id?: string | null;
    coverage_weeks?: number | null;
    sku_count?: number;
    editable?: boolean;
  };
  metrics: Record<string, MetricSeries>;
}

export interface Kpi {
  last_closed_week: string;
  plan_window_weeks: number;
  plan_units: number;
  fact_units: number;
  plan_completion_percent: number | null;
  plan_delta_units: number;
  next_week: string | null;
  next_week_forecast: number;
  short_skus: number;
  over_skus: number;
  sku_count: number;
}

export interface Matrix {
  level: Level;
  weeks: Week[];
  rows: Row[];
  totals: Row;
  kpi: Kpi;
  context: {
    last_closed_week: string;
    horizon_weeks: number;
    history_weeks: number;
    /** Позиций в показанной выборке; при отборе по статусу — меньше следующего. */
    sku_count: number;
    sku_count_total: number;
    /** Строк в выборке целиком: `rows` — окно из них, подгружаемое по мере прокрутки. */
    row_count: number;
    row_offset: number;
    row_limit: number | null;
    /**
     * Пары всей выборки, а не только пришедшего окна: по ним работают «отметить все»
     * и массовая правка. На уровнях групп правки нет, и список приходит пустым.
     */
    pair_ids: number[] | null;
  };
}

export interface Store {
  id: number;
  code: string;
  name: string;
  marketplace: string;
  is_active: boolean;
}

export interface Category {
  id: number;
  name: string;
  niche: string | null;
  abc_class: string | null;
}

export interface PurchaseRow {
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  category: string | null;
  supplier: string | null;
  current_stock: number;
  needed: boolean;
  shortage_week: string | null;
  order_date: string | null;
  arrival_week: string | null;
  qty: number;
  qty_before_constraints: number;
  cost: number;
  volume_m3: number;
  safety_stock: number;
  is_overdue: boolean;
  reason: string;
  /** Разбор количества: приходит из расчёта, экран его только показывает. */
  coverage_from: string | null;
  coverage_to: string | null;
  coverage_weeks_used: number;
  window_demand: number;
  window_transit: number;
  stock_before: number;
  params: {
    lead_time_days: number;
    moq: number;
    pack_size: number;
    coverage_weeks: number;
    safety_weeks: number;
    cost_price: number;
    volume_m3: number;
  };
}

export interface PurchasePlan {
  rows: PurchaseRow[];
  total_qty: number;
  total_cost: number;
  total_volume_m3: number;
  overdue_count: number;
}

export type OrderStatus = 'draft' | 'placed' | 'received' | 'cancelled';

export interface OrderLine {
  id: number;
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  qty: number;
  arrival_week: string;
  cost: number;
  volume_m3: number;
}

export interface Order {
  id: number;
  status: OrderStatus;
  status_label: string;
  supplier: string | null;
  note: string | null;
  created_at: string;
  created_by: string | null;
  placed_at: string | null;
  closed_at: string | null;
  lines_count: number;
  total_qty: number;
  total_cost: number;
  total_volume_m3: number;
  /** Статусы, в которые заказ можно перевести прямо сейчас. */
  next_statuses: OrderStatus[];
  lines: OrderLine[];
}

export interface OrderDraft {
  lines: { pair_id: number; qty: number; arrival_week: string }[];
  note?: string;
}

export type BulkOperation = 'scale' | 'delta' | 'set' | 'clear' | 'move';

export interface BulkRequest {
  pair_ids: number[];
  weeks: string[];
  operation: BulkOperation;
  value?: number;
  target_week?: string;
  author?: string;
}

export interface BulkResult {
  operation: BulkOperation;
  updated_cells: number;
  affected_pairs: number;
  skipped_closed: number;
  moved_units: number;
  weeks: string[];
  target_week: string | null;
}

export interface JournalEntry {
  id: number;
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  category: string | null;
  week: string;
  field: string;
  field_label: string;
  operation: string;
  batch_id: string | null;
  old_value: number | null;
  new_value: number | null;
  author: string | null;
  created_at: string;
}

export interface JournalPage {
  total: number;
  limit: number;
  offset: number;
  entries: JournalEntry[];
}

export interface JournalBatch {
  batch_id: string;
  operation: string;
  author: string | null;
  created_at: string;
  cells: number;
  pairs: number;
  week_from: string;
  week_to: string;
}

export interface JournalBatchPage {
  total: number;
  limit: number;
  offset: number;
  batches: JournalBatch[];
}

export interface JournalQuery {
  storeId: number | null;
  categoryId: number | null;
  search: string;
  field?: string;
  author?: string;
  batchId?: string;
}

export interface BasePotentialRow {
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  category: string | null;
  base_potential: number;
  fact_weeks: number;
  avg_fact_units: number | null;
  suggested_potential: number | null;
  excluded_weeks: number;
}

export interface SeasonalityRow {
  category_id: number;
  category: string;
  /** Ключ — номер ISO-недели, 1..53; в JSON приходит строкой. */
  coefficients: Record<string, number>;
}

export interface MatrixFilters {
  level: Level;
  storeId: number | null;
  categoryId: number | null;
  search: string;
  /** Отбор строк по статусу; считается после расчёта, поэтому только для матрицы. */
  rowStatus?: Status | null;
}
