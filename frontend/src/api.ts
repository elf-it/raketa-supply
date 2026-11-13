import type { Category, Matrix, MatrixFilters, PurchasePlan, Store } from './types';

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

  syncWb: () => request<unknown>('/sync/wb', { method: 'POST' }),
};
