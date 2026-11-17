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

export interface Supplier {
  id: number;
  name: string;
  lead_time_days: number;
  moq: number;
  product_count: number;
}

export interface Product {
  id: number;
  vendor_code: string;
  title: string;
  category: string | null;
  supplier: string | null;
  /** Действует сейчас; череда изменений — в истории цен (`PriceCard`). */
  cost_price: number;
  price: number;
  volume_m3: number;
  lead_time_days: number | null;
  moq: number | null;
  pack_size: number | null;
  /** С какой даты действуют нынешние цены; пусто — истории цен нет. */
  price_valid_from: string | null;
  category_id: number | null;
  supplier_id: number | null;
  coverage_weeks: number | null;
  safety_weeks: number | null;
  /** Снятый с продажи товар выпадает из расчёта вместе со своими парами. */
  is_active: boolean;
  /** В скольких кабинетах товар заведён: без пары он нигде не считается. */
  store_count: number;
}

/** Состояние загрузки одного кабинета (задача G-4). */
export interface StoreSync {
  store_id: number;
  store_code: string;
  store_name: string;
  /** ok · failed · stale · never */
  status: string;
  finished_at: string | null;
  facts_upserted: number;
  message: string | null;
}

export interface SyncHealth {
  /** ok · degraded */
  status: string;
  stale_after_hours: number;
  stores: StoreSync[];
}

/** Номенклатура кабинета, которой нет в каталоге (задача B-10). */
export interface Unmatched {
  store_id: number;
  store_code: string;
  store_name: string;
  nm_id: string;
  first_seen: string;
  last_seen: string;
  /** В скольких прогонах загрузки номер встретился. */
  times_seen: number;
}

/** Подсказка о дубле: решает человек, система только показывает похожих. */
export interface DuplicateHint {
  reason: string;
  left_id: number;
  left_code: string;
  left_title: string;
  left_pairs: number;
  right_id: number;
  right_code: string;
  right_title: string;
  right_pairs: number;
}

/** Счётчик с примерами: по одному числу не понять, что именно произойдёт. */
export interface CatalogTally {
  count: number;
  examples: string[];
}

/** Что файл номенклатуры изменит или изменил (задача B-15). */
export interface CatalogImport {
  applied: boolean;
  rows_read: number;
  total_changes: number;
  new_categories: CatalogTally;
  new_suppliers: CatalogTally;
  new_products: CatalogTally;
  updated_products: CatalogTally;
  new_pairs: CatalogTally;
  updated_pairs: CatalogTally;
  price_records: CatalogTally;
  /** Строки, которые в работу не взяли, — с номером строки файла и причиной. */
  issues: CatalogTally;
}

/** Товар в кабинете — минимальная расчётная единица (задача B-14). */
export interface Pair {
  id: number;
  store_id: number;
  store_code: string;
  store_name: string;
  store_active: boolean;
  /** Номер номенклатуры кабинета: по нему загрузка WB находит эту пару. */
  nm_id: string | null;
  barcode: string | null;
  base_potential: number;
  is_active: boolean;
  /** Сколько фактов уже привязано: по нему видно, можно ли пару удалить. */
  fact_count: number;
}

/** Запись истории цен: значения, действующие с даты. */
export interface PriceRecord {
  id: number;
  valid_from: string;
  cost_price: number;
  price: number;
  author: string | null;
  note: string | null;
  /** Когда запись завели — это не та же дата, что `valid_from`. */
  created_at: string;
}

export interface PriceCard {
  product_id: number;
  vendor_code: string;
  title: string;
  supplier: string | null;
  cost_price: number;
  price: number;
  valid_from: string | null;
  /** От новой записи к старой. */
  history: PriceRecord[];
}

export interface PurchaseRow {
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  category: string | null;
  supplier: string | null;
  /** По нему позиция сопоставляется с черновиком: тёзки в справочнике не помеха. */
  supplier_id: number | null;
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
  /**
   * Доходность вложенных в заказ денег, процентов годовых, и из чего она
   * сложилась (задача C-11). Пусто — считать не из чего: нет цены,
   * себестоимости или спроса; ноль означал бы «не окупается».
   */
  roi_percent: number | null;
  sell_out_weeks: number | null;
  cycle_weeks: number | null;
  params: {
    lead_time_days: number;
    moq: number;
    pack_size: number;
    coverage_weeks: number;
    safety_weeks: number;
    cost_price: number;
    /** Цена продажи: участвует только в ROI, на количество не влияет. */
    price: number;
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

/** Разбор загруженного файла: что он изменит и чего из него не взяли. */
export interface ImportChange {
  pair_id: number;
  store_name: string;
  vendor_code: string;
  title: string;
  week: string;
  old_value: number;
  new_value: number;
}

export interface ImportConflict extends ImportChange {
  /** Кто и когда поменял ячейку уже после выгрузки файла. */
  changed_at: string;
  changed_by: string | null;
}

export interface ImportIssue {
  reason: string;
  count: number;
  examples: string[];
}

export interface MatrixImport {
  exported_at: string;
  applied: boolean;
  rows_read: number;
  cells_read: number;
  change_count: number;
  conflict_count: number;
  applied_cells: number;
  batch_id: string | null;
  changes: ImportChange[];
  conflicts: ImportConflict[];
  issues: ImportIssue[];
}

/** Одно расхождение сверки с файлом заказчика (C-10). */
export interface ReconcileLine {
  vendor_code: string;
  store_name: string;
  title: string;
  week: string;
  ours: number;
  theirs: number;
  delta: number;
  ratio: number | null;
  reason: string;
  /** Номер строки в файле заказчика. */
  line: number;
}

export interface Reconcile {
  rows_read: number;
  compared: number;
  matched: number;
  diverged: number;
  /** Медиана отношения «наше / их» и доля ячеек рядом с ней. */
  median_ratio: number | null;
  ratio_share: number | null;
  status_compared: number;
  status_mismatched: number;
  status_examples: string[];
  /** Вывод словами — то, за чем сверку и запускают. */
  verdict: string;
  reasons: ImportIssue[];
  lines: ReconcileLine[];
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
  /** Себестоимость, записанная в строку при её создании. */
  cost_price: number;
  /** Действующая сейчас: расходится с записанной — цена изменилась после сборки. */
  cost_price_now: number;
}

export interface Order {
  id: number;
  status: OrderStatus;
  status_label: string;
  supplier: string | null;
  supplier_id: number | null;
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
