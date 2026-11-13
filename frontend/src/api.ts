import type {
  BasePotentialRow,
  Category,
  Matrix,
  MatrixFilters,
  PurchasePlan,
  SeasonalityRow,
  Store,
} from './types';

const BASE = '/api/v1';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status} ${response.statusText}: ${text.slice(0, 300)}`);
  }
  return (await response.json()) as T;
}

function query(filters: Partial<MatrixFilters>): string {
  const params = new URLSearchParams();
  if (filters.level) params.set('level', filters.level);
  if (filters.storeId) params.set('store_id', String(filters.storeId));
  if (filters.categoryId) params.set('category_id', String(filters.categoryId));
  if (filters.search?.trim()) params.set('search', filters.search.trim());
  return params.toString();
}

export const api = {
  stores: () => request<Store[]>('/catalog/stores'),
  categories: () => request<Category[]>('/catalog/categories'),

  matrix: (filters: MatrixFilters) => request<Matrix>(`/matrix?${query(filters)}`),

  purchase: (filters: Omit<MatrixFilters, 'level'>) =>
    request<PurchasePlan>(`/purchase?only_needed=true&${query(filters)}`),

  setForecast: (
    pairId: number,
    week: string,
    payload: {
      manual_forecast?: number;
      manual_adapted?: number;
      diff_percent?: number;
      clear?: string[];
    },
  ) =>
    request<unknown>(`/forecast/${pairId}/${week}`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),

  basePotential: (filters: Omit<MatrixFilters, 'level'>, factWeeks: number) =>
    request<BasePotentialRow[]>(
      `/reference/base-potential?fact_weeks=${factWeeks}&${query(filters)}`,
    ),

  setBasePotential: (pairId: number, value: number) =>
    request<BasePotentialRow>(`/reference/base-potential/${pairId}`, {
      method: 'PUT',
      body: JSON.stringify({ base_potential: value }),
    }),

  seasonality: () => request<SeasonalityRow[]>('/reference/seasonality'),

  setSeasonality: (categoryId: number, coefficients: Record<number, number>) =>
    request<SeasonalityRow>(`/reference/seasonality/${categoryId}`, {
      method: 'PUT',
      body: JSON.stringify({ coefficients }),
    }),

  syncWb: () => request<unknown>('/sync/wb', { method: 'POST' }),
};
