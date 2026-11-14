import type {
  BasePotentialRow,
  BulkRequest,
  BulkResult,
  Category,
  CurrentUser,
  JournalBatchPage,
  JournalPage,
  JournalQuery,
  Matrix,
  MatrixFilters,
  Order,
  OrderDraft,
  OrderStatus,
  PurchasePlan,
  Role,
  SeasonalityRow,
  Store,
  User,
} from './types';

const BASE = '/api/v1';

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Вызывается, когда сессия перестала действовать: сервер ответил 401.
 * Приложение подписывается на это и возвращает пользователя на экран входа.
 */
let onUnauthorized: (() => void) | null = null;

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

/** Пути, на которых 401 — нормальный ответ, а не потеря сессии. */
const AUTH_PATHS = ['/auth/login', '/auth/me'];

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
  if (!response.ok) {
    const text = await response.text();
    let detail = text.slice(0, 300);
    try {
      detail = (JSON.parse(text).detail as string) ?? detail;
    } catch {
      // Ответ не JSON — оставляем как есть.
    }
    if (response.status === 401 && !AUTH_PATHS.some((p) => path.startsWith(p))) {
      onUnauthorized?.();
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}

function query(filters: Partial<MatrixFilters>): string {
  const params = new URLSearchParams();
  if (filters.level) params.set('level', filters.level);
  if (filters.storeId) params.set('store_id', String(filters.storeId));
  if (filters.categoryId) params.set('category_id', String(filters.categoryId));
  if (filters.search?.trim()) params.set('search', filters.search.trim());
  if (filters.rowStatus) params.set('status', filters.rowStatus);
  return params.toString();
}

function journalQuery(query: JournalQuery, limit: number, offset: number): string {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query.storeId) params.set('store_id', String(query.storeId));
  if (query.categoryId) params.set('category_id', String(query.categoryId));
  if (query.search?.trim()) params.set('search', query.search.trim());
  if (query.field) params.set('field', query.field);
  if (query.author?.trim()) params.set('author', query.author.trim());
  if (query.batchId) params.set('batch_id', query.batchId);
  return params.toString();
}

export const api = {
  me: () => request<CurrentUser>('/auth/me'),

  login: (email: string, password: string) =>
    request<CurrentUser>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  logout: () => request<unknown>('/auth/logout', { method: 'POST' }),

  // Управление пользователями. Ручки закрыты ролью на сервере: вкладка, спрятанная
  // от не-администратора, — удобство, а не защита.
  users: () => request<User[]>('/auth/users'),

  createUser: (payload: { email: string; full_name: string; role: Role; password: string }) =>
    request<User>('/auth/users', { method: 'POST', body: JSON.stringify(payload) }),

  updateUser: (
    userId: number,
    payload: { full_name?: string; role?: Role; is_active?: boolean; password?: string },
  ) => request<User>(`/auth/users/${userId}`, { method: 'PUT', body: JSON.stringify(payload) }),

  stores: () => request<Store[]>('/catalog/stores'),
  categories: () => request<Category[]>('/catalog/categories'),

  /**
   * Матрица с окном строк. Итоги, KPI и счётчики в ответе — по всей выборке,
   * режутся только строки; `window` пусто — вся выборка, как для выгрузки.
   */
  matrix: (filters: MatrixFilters, window?: { offset?: number; limit?: number }) => {
    const params = new URLSearchParams(query(filters));
    if (window?.offset) params.set('offset', String(window.offset));
    if (window?.limit) params.set('limit', String(window.limit));
    return request<Matrix>(`/matrix?${params.toString()}`);
  },

  purchase: (filters: Omit<MatrixFilters, 'level'>, onlyNeeded: boolean) =>
    request<PurchasePlan>(`/purchase?only_needed=${onlyNeeded}&${query(filters)}`),

  // Выгрузки — обычные ссылки, а не fetch: файл сохраняет браузер, с именем,
  // которое прислал сервер. Фильтры те же, что у запроса данных, иначе в файле
  // окажется не та выборка, которую человек видит на экране.
  matrixExportUrl: (filters: MatrixFilters) => `${BASE}/matrix/export?${query(filters)}`,

  purchaseExportUrl: (filters: Omit<MatrixFilters, 'level'>, onlyNeeded: boolean) =>
    `${BASE}/purchase/export?only_needed=${onlyNeeded}&${query(filters)}`,

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

  bulkAdjust: (payload: BulkRequest) =>
    request<BulkResult>('/forecast/bulk', {
      method: 'POST',
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

  journalEntries: (query: JournalQuery, limit: number, offset: number) =>
    request<JournalPage>(`/forecast/log?${journalQuery(query, limit, offset)}`),

  journalBatches: (query: JournalQuery, limit: number, offset: number) =>
    request<JournalBatchPage>(`/forecast/log/batches?${journalQuery(query, limit, offset)}`),

  seasonality: () => request<SeasonalityRow[]>('/reference/seasonality'),

  setSeasonality: (categoryId: number, coefficients: Record<number, number>) =>
    request<SeasonalityRow>(`/reference/seasonality/${categoryId}`, {
      method: 'PUT',
      body: JSON.stringify({ coefficients }),
    }),

  orders: (status?: OrderStatus) =>
    request<Order[]>(`/orders${status ? `?status=${status}` : ''}`),

  createOrder: (draft: OrderDraft) =>
    request<Order>('/orders', { method: 'POST', body: JSON.stringify(draft) }),

  // Правка черновика. Сервер отвечает заказом целиком, но экран всё равно
  // перечитывает список: правка меняет итоги, а они видны и в соседних строках.
  updateOrderLine: (
    orderId: number,
    lineId: number,
    payload: { qty?: number; arrival_week?: string },
  ) =>
    request<Order>(`/orders/${orderId}/lines/${lineId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),

  deleteOrderLine: (orderId: number, lineId: number) =>
    request<Order>(`/orders/${orderId}/lines/${lineId}`, { method: 'DELETE' }),

  setOrderNote: (orderId: number, note: string | null) =>
    request<Order>(`/orders/${orderId}`, { method: 'PATCH', body: JSON.stringify({ note }) }),

  setOrderStatus: (orderId: number, status: OrderStatus) =>
    request<Order>(`/orders/${orderId}/status`, {
      method: 'POST',
      body: JSON.stringify({ status }),
    }),

  syncWb: () => request<unknown>('/sync/wb', { method: 'POST' }),
};
