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
    sku_count: number;
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
}
